"""
S&P 500 Weekly Support/Resistance Screener
==========================================

Runs on GitHub Actions every weekday at 6 PM EST.

For each S&P 500 stock it builds WEEKLY candles and computes:
  * Two Anchored VWAPs (AVWAP):
      - anchored at the first weekly candle of the CURRENT year
      - anchored at the first weekly candle of the PREVIOUS year
  * EMA 9 and EMA 20 (the "EMA cloud" is the zone between them)

Detection (over the 3 most recent weekly candles = current + last 2):
  * SUPPORT    -> candle LOW touches within TOLERANCE of a level AND the
                  candle CLOSES above that level (bullish rejection).
  * RESISTANCE -> candle HIGH touches within TOLERANCE of a level AND the
                  candle CLOSES below that level (bearish rejection).

Levels tested: AVWAP(current year), AVWAP(previous year), EMA9, EMA20.
The EMA cloud counts if EITHER edge (EMA9 or EMA20) is touched.

Matches are emailed to the configured recipient with separate
Support and Resistance sections.
"""

from __future__ import annotations

import io
import os
import smtplib
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests
import yfinance as yf

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TOLERANCE = 0.01          # 1.0% band for an AVWAP "touch"
LOOKBACK_WEEKS = 2        # current weekly candle + last 1
MIN_PRICE = 50.0          # only stocks trading above this (last close)
MIN_MARKET_CAP = 10_000_000_000   # only stocks with market cap above $10B
REJECT_FRACTION = 2 / 3   # EMA-cloud rejection: close must be in this
                          # fraction (upper third for support / lower third
                          # for resistance) of the candle's range
EMA_FAST = 9
EMA_SLOW = 20
WEEKLY_PERIOD = "3y"      # enough history to anchor at start of previous year
BATCH_SIZE = 100          # tickers per yfinance download call
RECIPIENT = "avin.khurana18@gmail.com"

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
USER_AGENT = "Mozilla/5.0 (screener; +https://github.com)"


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #

@dataclass
class Hit:
    ticker: str
    kind: str                       # "support" or "resistance"
    price: float                    # latest close
    weeks_ago: int                  # 0 = current candle, 1 = last week, ...
    candle_date: str
    levels: list[str] = field(default_factory=list)  # which levels triggered
    sector: str = "Unknown"         # GICS sector


# --------------------------------------------------------------------------- #
# S&P 500 universe
# --------------------------------------------------------------------------- #

def get_sp500_universe() -> dict[str, str]:
    """
    Scrape the current S&P 500 constituents from Wikipedia, returning a
    mapping of {ticker: GICS sector}. yfinance uses '-' instead of '.'
    (e.g. BRK.B -> BRK-B).
    """
    resp = requests.get(WIKI_SP500, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]
    universe: dict[str, str] = {}
    for _, row in df.iterrows():
        sym = str(row["Symbol"]).strip().replace(".", "-")
        if not sym:
            continue
        sector = str(row.get("GICS Sector", "Unknown")).strip() or "Unknown"
        universe[sym] = sector
    return universe


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #

def anchored_vwap(df: pd.DataFrame, anchor_year: int) -> pd.Series | None:
    """
    Anchored VWAP over weekly candles, starting from the first weekly
    candle whose year == anchor_year. Returns a Series aligned to df.index,
    or None if the anchor year is not present in the data.
    """
    mask = df.index.year == anchor_year
    if not mask.any():
        return None
    anchor_idx = df.index[mask][0]
    seg = df.loc[anchor_idx:]
    typical = (seg["High"] + seg["Low"] + seg["Close"]) / 3.0
    cum_pv = (typical * seg["Volume"]).cumsum()
    cum_v = seg["Volume"].cumsum().replace(0, pd.NA)
    vwap = cum_pv / cum_v
    return vwap.reindex(df.index)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Attach EMA9, EMA20 and the two anchored VWAPs to the weekly frame."""
    now_year = datetime.now(timezone.utc).year
    df = df.copy()
    df["EMA_FAST"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA_SLOW"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["AVWAP_CUR"] = anchored_vwap(df, now_year)
    df["AVWAP_PREV"] = anchored_vwap(df, now_year - 1)
    return df


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def _touch(value: float, level: float) -> bool:
    """True if `value` is within TOLERANCE of `level`."""
    if level is None or pd.isna(level) or level <= 0:
        return False
    return abs(value - level) / level <= TOLERANCE


def _avwap_support(low, close, level) -> bool:
    """AVWAP support: low touches within TOLERANCE of the level, closes above."""
    return _touch(low, level) and close > level


def _avwap_resistance(high, close, level) -> bool:
    """AVWAP resistance: high touches within TOLERANCE, closes below."""
    return _touch(high, level) and close < level


def _cloud_support(low, high, close, cloud_lo, cloud_hi) -> bool:
    """
    EMA-cloud support (strict rejection): the wick pulls back INTO the cloud
    zone (low lands between the cloud bottom - TOLERANCE and the cloud top),
    the candle closes back ABOVE the whole cloud, and the close sits in the
    upper third of the range. This is a genuine bounce off the cloud, not a
    green week that merely floated above it or crashed straight through it.
    """
    rng = high - low
    if rng <= 0:
        return False
    entered_cloud = low <= cloud_hi                     # wicked into the cloud
    held_cloud = low >= cloud_lo * (1 - TOLERANCE)      # didn't blow through it
    closed_above = close > cloud_hi                     # reclaimed the cloud
    rejection = (close - low) / rng >= REJECT_FRACTION  # bounce, not drift
    return entered_cloud and held_cloud and closed_above and rejection


def _cloud_resistance(low, high, close, cloud_lo, cloud_hi) -> bool:
    """
    EMA-cloud resistance (strict rejection): the wick pushes UP INTO the cloud
    zone (high lands between the cloud bottom and the cloud top + TOLERANCE),
    the candle closes back BELOW the whole cloud, and the close sits in the
    lower third of the range.
    """
    rng = high - low
    if rng <= 0:
        return False
    entered_cloud = high >= cloud_lo                    # wicked into the cloud
    held_cloud = high <= cloud_hi * (1 + TOLERANCE)     # didn't break above it
    closed_below = close < cloud_lo                     # rejected the cloud
    rejection = (high - close) / rng >= REJECT_FRACTION
    return entered_cloud and held_cloud and closed_below and rejection


def evaluate(ticker: str, df: pd.DataFrame) -> tuple[Hit | None, Hit | None]:
    """
    Inspect the last LOOKBACK_WEEKS weekly candles of one stock and return
    (support_hit, resistance_hit); either may be None.

    AVWAP levels use the 1% proximity rule; the EMA cloud uses strict rejection.
    """
    df = add_indicators(df)
    if len(df) < EMA_SLOW:
        return None, None

    last_close = float(df["Close"].iloc[-1])
    if pd.isna(last_close) or last_close < MIN_PRICE:
        return None, None

    recent = df.tail(LOOKBACK_WEEKS)

    avwaps = [
        ("AVWAP (current year)", "AVWAP_CUR"),
        ("AVWAP (previous year)", "AVWAP_PREV"),
    ]

    # --- Trend-structure confirmation (measured on the LATEST weekly candle) ---
    # The level giving support must be the UPPER of the two structures (so the
    # other one sits below as deeper support); mirrored for resistance. In every
    # case BOTH AVWAPs must sit on the same side of the 9 EMA:
    #   * cloud support    -> BOTH AVWAPs BELOW the 9 EMA
    #   * cloud resistance -> BOTH AVWAPs ABOVE the 9 EMA
    #   * AVWAP support    -> the 9 EMA BELOW BOTH AVWAPs (i.e. both AVWAPs above)
    #   * AVWAP resistance -> the 9 EMA ABOVE BOTH AVWAPs (i.e. both AVWAPs below)
    # (If an AVWAP is missing due to short history, only the available one(s) count.)
    latest = df.iloc[-1]
    ema9_now = float(latest["EMA_FAST"])
    avwap_now = {col: (float(latest[col]) if not pd.isna(latest[col]) else None)
                 for _, col in avwaps}
    existing_avwaps = [v for v in avwap_now.values() if v is not None]

    both_avwaps_below = all(v < ema9_now for v in existing_avwaps)
    both_avwaps_above = all(v > ema9_now for v in existing_avwaps)

    cloud_sup_ok = both_avwaps_below   # cloud is the upper support structure
    cloud_res_ok = both_avwaps_above   # cloud is the lower resistance structure
    avwap_sup_ok = both_avwaps_above   # AVWAPs are the upper support structure
    avwap_res_ok = both_avwaps_below   # AVWAPs are the lower resistance structure

    support_levels: dict[str, tuple[int, str]] = {}    # label -> (weeks_ago, date)
    resistance_levels: dict[str, tuple[int, str]] = {}

    def record(store, label, weeks_ago, cdate):
        prev = store.get(label)
        if prev is None or weeks_ago < prev[0]:
            store[label] = (weeks_ago, cdate)

    n = len(recent)
    for pos, (_, row) in enumerate(recent.iterrows()):
        weeks_ago = (n - 1) - pos
        low, high, close = float(row["Low"]), float(row["High"]), float(row["Close"])
        cdate = row.name.date().isoformat()

        # --- AVWAP levels: proximity rule + structure confirmation ---
        for label, col in avwaps:
            level = row[col]
            if pd.isna(level):
                continue
            level = float(level)
            if avwap_sup_ok and _avwap_support(low, close, level):
                record(support_levels, label, weeks_ago, cdate)
            if avwap_res_ok and _avwap_resistance(high, close, level):
                record(resistance_levels, label, weeks_ago, cdate)

        # --- EMA cloud (zone between EMA9 and EMA20): strict rejection + confirm ---
        ema_f, ema_s = row["EMA_FAST"], row["EMA_SLOW"]
        if not (pd.isna(ema_f) or pd.isna(ema_s)):
            cloud_lo, cloud_hi = sorted((float(ema_f), float(ema_s)))
            if cloud_sup_ok and _cloud_support(low, high, close, cloud_lo, cloud_hi):
                record(support_levels, "EMA cloud", weeks_ago, cdate)
            if cloud_res_ok and _cloud_resistance(low, high, close, cloud_lo, cloud_hi):
                record(resistance_levels, "EMA cloud", weeks_ago, cdate)

    support_hit = _build_hit(ticker, "support", last_close, support_levels)
    resistance_hit = _build_hit(ticker, "resistance", last_close, resistance_levels)
    return support_hit, resistance_hit


def _build_hit(ticker, kind, price, levels_map) -> Hit | None:
    if not levels_map:
        return None
    # collapse the two EMA-cloud edges into a single label
    labels = list(levels_map.keys())
    if any(l.startswith("EMA cloud") for l in labels):
        labels = [l for l in labels if not l.startswith("EMA cloud")]
        labels.append("EMA cloud")
    weeks_ago = min(v[0] for v in levels_map.values())
    date = min(levels_map.values(), key=lambda v: v[0])[1]
    return Hit(
        ticker=ticker,
        kind=kind,
        price=price,
        weeks_ago=weeks_ago,
        candle_date=date,
        levels=labels,
    )


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def download_weekly(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Download weekly OHLCV for all tickers, returned per-ticker."""
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f"Downloading batch {i // BATCH_SIZE + 1} "
              f"({len(batch)} tickers)...", flush=True)
        try:
            data = yf.download(
                batch,
                period=WEEKLY_PERIOD,
                interval="1wk",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  batch download failed: {exc}", flush=True)
            continue

        for t in batch:
            try:
                sub = data[t] if len(batch) > 1 else data
                sub = sub.dropna(how="all")
                if not sub.empty:
                    out[t] = sub
            except (KeyError, Exception):  # noqa: BLE001
                continue
        time.sleep(1)  # be polite to Yahoo
    return out


def _market_cap(ticker: str) -> float | None:
    """Fetch a single ticker's market cap via yfinance fast_info."""
    try:
        mc = yf.Ticker(ticker).fast_info.market_cap
        return float(mc) if mc else None
    except Exception:  # noqa: BLE001
        return None


def fetch_market_caps(tickers: list[str]) -> dict[str, float]:
    """
    Fetch market caps for the given tickers concurrently. Only tickers with a
    successfully retrieved value are included in the result (fetch failures are
    omitted, so the caller decides how to treat unknowns).
    """
    caps: dict[str, float] = {}
    if not tickers:
        return caps
    print(f"Fetching market caps for {len(tickers)} candidate tickers...",
          flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_market_cap, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            mc = fut.result()
            if mc is not None:
                caps[t] = mc
    return caps


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

GREEN = "#1a7f37"   # support
RED = "#cf222e"     # resistance


def _hit_row(h: Hit) -> str:
    color = GREEN if h.kind == "support" else RED
    signal = "Support" if h.kind == "support" else "Resistance"
    when = "current week" if h.weeks_ago == 0 else f"{h.weeks_ago}w ago"
    return (
        "<tr>"
        f'<td style="padding:8px;border-bottom:1px solid #eee;border-left:4px solid {color};'
        f'font-weight:bold;color:{color};">{h.ticker}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;color:{color};'
        f'font-weight:bold;">{signal}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;">${h.price:,.2f}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;">{", ".join(h.levels)}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;">{when}<br>'
        f'<span style="color:#999;font-size:12px;">{h.candle_date}</span></td>'
        "</tr>"
    )


def _sector_block(sector: str, hits: list[Hit]) -> str:
    # support first (green), then resistance (red); each most-recent first
    hits = sorted(hits, key=lambda h: (0 if h.kind == "support" else 1,
                                       h.weeks_ago, h.ticker))
    s_ct = sum(1 for h in hits if h.kind == "support")
    r_ct = sum(1 for h in hits if h.kind == "resistance")
    rows = "\n".join(_hit_row(h) for h in hits)
    return f"""
        <h2 style="margin-top:30px;margin-bottom:6px;border-bottom:2px solid #ddd;
                   padding-bottom:4px;font-size:18px;">{sector}
          <span style="font-size:13px;font-weight:normal;">
            &nbsp;<span style="color:{GREEN};">{s_ct} support</span> &middot;
            <span style="color:{RED};">{r_ct} resistance</span></span></h2>
        <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;">
          <tr style="background:#f4f4f4;color:#555;text-align:left;">
            <th style="padding:8px;">Ticker</th>
            <th style="padding:8px;">Signal</th>
            <th style="padding:8px;">Last Close</th>
            <th style="padding:8px;">Level(s)</th>
            <th style="padding:8px;">Touched</th>
          </tr>
          {rows}
        </table>"""


def build_email_html(support: list[Hit], resistance: list[Hit], scanned: int) -> str:
    date_str = datetime.now(timezone.utc).astimezone().strftime("%A, %B %d, %Y")

    by_sector: dict[str, list[Hit]] = defaultdict(list)
    for h in support + resistance:
        by_sector[h.sector or "Unknown"].append(h)

    if by_sector:
        blocks = "\n".join(_sector_block(sec, by_sector[sec])
                           for sec in sorted(by_sector))
    else:
        blocks = '<p style="color:#888;margin-top:24px;">No matches today.</p>'

    return f"""
    <html><body style="font-family:Arial,sans-serif;color:#222;max-width:760px;margin:auto;">
      <h1 style="margin-bottom:4px;">S&amp;P 500 Weekly S/R Screener</h1>
      <p style="color:#666;margin-top:0;">{date_str} &middot; {scanned} stocks scanned &middot;
         weekly candles &middot; AVWAP (2 anchors) + EMA 9/20 cloud &middot;
         {TOLERANCE * 100:.1f}% tolerance &middot; last {LOOKBACK_WEEKS} weeks &middot;
         price &gt; ${MIN_PRICE:g} &middot; mcap &gt; ${MIN_MARKET_CAP/1e9:g}B</p>
      <p style="margin-top:0;font-size:14px;">
        <b style="color:{GREEN};">&#9632; {len(support)} support</b> &nbsp;&middot;&nbsp;
        <b style="color:{RED};">&#9632; {len(resistance)} resistance</b>
        &nbsp;&middot;&nbsp;<span style="color:#888;">grouped by GICS sector</span></p>
      {blocks}
      <p style="color:#999;font-size:12px;margin-top:30px;">
        AVWAP support/resistance = weekly wick within {TOLERANCE*100:.1f}% of the AVWAP
        and close on the supporting/resisting side.
        EMA-cloud = strict rejection: the wick pulls into the EMA9&ndash;EMA20 zone and the
        candle closes back beyond it with the close in the outer third of the range.
        Structure filter: cloud signals require both AVWAPs on the far side of the 9 EMA;
        AVWAP signals require the 9 EMA on the far side of both AVWAPs.
        Checked over the last {LOOKBACK_WEEKS} weekly candles.
        Automated screener &middot; not financial advice.</p>
    </body></html>"""


def send_email(html: str, support: list[Hit], resistance: list[Hit]) -> None:
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_APP_PASSWORD")
    if not user or not password:
        print("EMAIL_USER / EMAIL_APP_PASSWORD not set - skipping send.", flush=True)
        print(html)
        return

    msg = MIMEMultipart("alternative")
    today = datetime.now(timezone.utc).astimezone().strftime("%b %d")
    msg["Subject"] = (f"S&P500 Weekly S/R - {today}: "
                      f"{len(support)} support, {len(resistance)} resistance")
    msg["From"] = user
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [RECIPIENT], msg.as_string())
    print(f"Email sent to {RECIPIENT}.", flush=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    print("Fetching S&P 500 universe...", flush=True)
    universe = get_sp500_universe()   # {ticker: GICS sector}
    tickers = list(universe)
    print(f"{len(tickers)} tickers across {len(set(universe.values()))} sectors.",
          flush=True)

    data = download_weekly(tickers)
    print(f"Got weekly data for {len(data)} tickers.", flush=True)

    support: list[Hit] = []
    resistance: list[Hit] = []
    for t, df in data.items():
        try:
            s, r = evaluate(t, df)
        except Exception as exc:  # noqa: BLE001
            print(f"  {t}: eval error {exc}", flush=True)
            continue
        sector = universe.get(t, "Unknown")
        if s:
            s.sector = sector
            support.append(s)
        if r:
            r.sector = sector
            resistance.append(r)

    print(f"Support hits: {len(support)} | Resistance hits: {len(resistance)} "
          f"(price >= ${MIN_PRICE:g})", flush=True)

    # Market-cap filter: only fetch caps for stocks that actually produced a hit.
    hit_tickers = {h.ticker for h in support} | {h.ticker for h in resistance}
    caps = fetch_market_caps(sorted(hit_tickers))

    def passes_mcap(h: Hit) -> bool:
        # Fail-open: keep a stock if its market cap could not be retrieved,
        # rather than dropping a valid signal on a transient data error.
        mc = caps.get(h.ticker)
        return mc is None or mc >= MIN_MARKET_CAP

    dropped = sum(1 for h in support + resistance
                  if caps.get(h.ticker) is not None
                  and caps[h.ticker] < MIN_MARKET_CAP)
    unknown = len(hit_tickers) - len(caps)
    support = [h for h in support if passes_mcap(h)]
    resistance = [h for h in resistance if passes_mcap(h)]
    print(f"Market-cap filter (>= ${MIN_MARKET_CAP/1e9:g}B): dropped {dropped}, "
          f"{unknown} unknown (kept). "
          f"Final: {len(support)} support | {len(resistance)} resistance.",
          flush=True)

    html = build_email_html(support, resistance, len(data))
    send_email(html, support, resistance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
