"""
S&P 500 Weekly Support/Resistance Screener
==========================================

Runs on GitHub Actions every weekday at 6 PM EST.

For each S&P 500 stock it builds WEEKLY candles and computes:
  * Two Anchored VWAPs (AVWAP):
      - anchored at the first weekly candle of the CURRENT year
      - anchored at the first weekly candle of the PREVIOUS year
  * EMA 9 and EMA 20 (the "EMA cloud" is the zone between them)

Detection (over the LOOKBACK_WEEKS most recent weekly candles):
  * SUPPORT    -> candle LOW touches within TOLERANCE of a level AND the
                  candle CLOSES above that level (bullish rejection).
  * RESISTANCE -> candle HIGH touches within TOLERANCE of a level AND the
                  candle CLOSES below that level (bearish rejection).

Levels tested: AVWAP(current year), AVWAP(previous year), EMA9, EMA20.
The EMA cloud counts if EITHER edge (EMA9 or EMA20) is touched.

CONFLUENCE: a stock that shows the SAME signal (support or resistance) on
BOTH the EMA cloud AND at least one AVWAP within the lookback window is
reported in a separate section at the end. The trend-structure filter is
NOT applied here — it requires the AVWAPs and the cloud on opposite sides
of price and would make co-occurrence impossible; an AVWAP sitting inside
or near the cloud is precisely what forms the confluence zone.

Each hit is also graded on FUNDAMENTAL QUALITY (wheel-strategy suitability):
net margin, return on equity, debt/equity and dividend — stocks passing the
gate are flagged "Strong" in the email so CSP/CC candidates stand out.

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
MIN_MARKET_CAP = 100_000_000_000  # only stocks with market cap above $100B
REJECT_FRACTION = 2 / 3   # EMA-cloud rejection: close must be in this
                          # fraction (upper third for support / lower third
                          # for resistance) of the candle's range
EMA_FAST = 9
EMA_SLOW = 20
WEEKLY_PERIOD = "3y"      # enough history to anchor at start of previous year
BATCH_SIZE = 100          # tickers per yfinance download call
RECIPIENT = "avin.khurana18@gmail.com"

# Fundamental-quality gate (wheel suitability). A hit is "Strong" when it
# fails at most one evaluable check and passes at least two. Checks whose
# data is unavailable (e.g. debt/equity for banks) are excluded, not failed.
QUALITY_MIN_MARGIN = 0.05   # net profit margin >= 5%
QUALITY_MIN_ROE = 0.10      # return on equity >= 10%
QUALITY_MAX_DE = 200.0      # debt/equity <= 200% (yfinance reports percent)

# Relative strength (sector_rs_scanner methodology): % outperformance on
# DAILY closes over 1-month and 3-month lookbacks, shown for sector-vs-SPY
# (in the sector heading), stock-vs-sector and stock-vs-SPY (per row).
RS_LOOKBACKS = {"1M": 21, "3M": 63}   # label -> trading days
RS_PERIOD = "4mo"                     # daily history; covers 3M with buffer
BENCHMARK = "SPY"
SECTOR_ETF = {              # GICS sector -> SPDR sector ETF
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
}

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
    quality: str = "n/a"            # e.g. "3/4" (checks passed / evaluable)
    quality_fails: list[str] = field(default_factory=list)  # failed checks
    strong: bool = False            # passes the fundamental-quality gate
    # relative strength in % points, keyed by lookback label ("1M", "3M")
    rs_sector_spy: dict[str, float] = field(default_factory=dict)
    rs_stock_sector: dict[str, float] = field(default_factory=dict)
    rs_stock_spy: dict[str, float] = field(default_factory=dict)


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


def pct_change_over(prices: pd.Series | None, n: int) -> float | None:
    """% change over the last n trading days (sector_rs_scanner logic)."""
    if prices is None:
        return None
    prices = prices.dropna()
    if len(prices) <= n:
        return None
    return float((prices.iloc[-1] / prices.iloc[-n - 1] - 1) * 100)


def download_daily_closes(tickers: list[str]) -> pd.DataFrame:
    """Daily adjusted closes for the RS lookbacks, one column per ticker."""
    data = yf.download(tickers, period=RS_PERIOD, interval="1d",
                       auto_adjust=True, progress=False)["Close"]
    if isinstance(data, pd.Series):          # single-ticker shape
        data = data.to_frame(tickers[0])
    return data


def annotate_relative_strength(hits: list[Hit], closes: pd.DataFrame) -> None:
    """
    Fill each hit's three relative-strength legs, per RS_LOOKBACKS horizon
    (% return difference on daily closes):
    sector ETF vs SPY, stock vs sector ETF, stock vs SPY.
    """
    spy = closes.get(BENCHMARK)
    for h in hits:
        stock = closes.get(h.ticker)
        etf = SECTOR_ETF.get(h.sector)
        sec = closes.get(etf) if etf else None
        for label, n in RS_LOOKBACKS.items():
            spy_ret = pct_change_over(spy, n)
            sec_ret = pct_change_over(sec, n)
            stk_ret = pct_change_over(stock, n)
            if sec_ret is not None and spy_ret is not None:
                h.rs_sector_spy[label] = sec_ret - spy_ret
            if stk_ret is not None and sec_ret is not None:
                h.rs_stock_sector[label] = stk_ret - sec_ret
            if stk_ret is not None and spy_ret is not None:
                h.rs_stock_spy[label] = stk_ret - spy_ret


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


def evaluate(
    ticker: str, df: pd.DataFrame,
) -> tuple[Hit | None, Hit | None, Hit | None, Hit | None]:
    """
    Inspect the last LOOKBACK_WEEKS weekly candles of one stock and return
    (support_hit, resistance_hit, confluence_support, confluence_resistance);
    any may be None.

    AVWAP levels use the 1% proximity rule; the EMA cloud uses strict rejection.
    Confluence hits use the same raw touch/rejection rules but WITHOUT the
    trend-structure filter (which forbids cloud+AVWAP co-occurrence) and
    require both level families to fire on the same side.
    """
    df = add_indicators(df)
    if len(df) < EMA_SLOW:
        return None, None, None, None

    last_close = float(df["Close"].iloc[-1])
    if pd.isna(last_close) or last_close < MIN_PRICE:
        return None, None, None, None

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
    # raw (structure-filter-free) touches, used only for confluence detection
    conf_support_levels: dict[str, tuple[int, str]] = {}
    conf_resistance_levels: dict[str, tuple[int, str]] = {}

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
            if _avwap_support(low, close, level):
                record(conf_support_levels, label, weeks_ago, cdate)
                if avwap_sup_ok:
                    record(support_levels, label, weeks_ago, cdate)
            if _avwap_resistance(high, close, level):
                record(conf_resistance_levels, label, weeks_ago, cdate)
                if avwap_res_ok:
                    record(resistance_levels, label, weeks_ago, cdate)

        # --- EMA cloud (zone between EMA9 and EMA20): strict rejection + confirm ---
        ema_f, ema_s = row["EMA_FAST"], row["EMA_SLOW"]
        if not (pd.isna(ema_f) or pd.isna(ema_s)):
            cloud_lo, cloud_hi = sorted((float(ema_f), float(ema_s)))
            if _cloud_support(low, high, close, cloud_lo, cloud_hi):
                record(conf_support_levels, "EMA cloud", weeks_ago, cdate)
                if cloud_sup_ok:
                    record(support_levels, "EMA cloud", weeks_ago, cdate)
            if _cloud_resistance(low, high, close, cloud_lo, cloud_hi):
                record(conf_resistance_levels, "EMA cloud", weeks_ago, cdate)
                if cloud_res_ok:
                    record(resistance_levels, "EMA cloud", weeks_ago, cdate)

    support_hit = _build_hit(ticker, "support", last_close, support_levels)
    resistance_hit = _build_hit(ticker, "resistance", last_close, resistance_levels)
    conf_support_hit = (_build_hit(ticker, "support", last_close, conf_support_levels)
                        if _is_confluence(conf_support_levels) else None)
    conf_resistance_hit = (_build_hit(ticker, "resistance", last_close,
                                      conf_resistance_levels)
                           if _is_confluence(conf_resistance_levels) else None)
    return support_hit, resistance_hit, conf_support_hit, conf_resistance_hit


def _is_confluence(levels_map: dict) -> bool:
    """Both level families fired: the EMA cloud AND at least one AVWAP."""
    has_cloud = any(l.startswith("EMA cloud") for l in levels_map)
    has_avwap = any(l.startswith("AVWAP") for l in levels_map)
    return has_cloud and has_avwap


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
                # group_by="ticker" yields MultiIndex columns even for a
                # single-ticker batch, so always select the ticker level.
                sub = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                sub = sub.dropna(how="all")
                if not sub.empty:
                    out[t] = sub
            except Exception:  # noqa: BLE001
                continue
        time.sleep(1)  # be polite to Yahoo
    return out


@dataclass
class Fundamentals:
    mcap: float | None = None
    quality: str = "n/a"                     # "passed/evaluable", e.g. "3/4"
    fails: list[str] = field(default_factory=list)
    strong: bool = False


def _fundamentals(ticker: str) -> Fundamentals:
    """
    Fetch market cap + a fundamental-quality grade for one ticker.

    Quality checks (pass / fail / n-a when the field is unavailable):
      * net profit margin >= QUALITY_MIN_MARGIN
      * return on equity  >= QUALITY_MIN_ROE   (n/a for negative-equity names)
      * debt / equity     <= QUALITY_MAX_DE    (n/a for banks)
      * pays a dividend                        (None = non-payer = fail)
    "Strong" = at most one failed check and at least two passed.
    """
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001
        return Fundamentals()

    mcap = info.get("marketCap")
    mcap = float(mcap) if mcap else None

    checks: list[tuple[str, bool | None]] = []   # (fail label, pass/fail/None)
    margin = info.get("profitMargins")
    checks.append(("low margin",
                   None if margin is None else margin >= QUALITY_MIN_MARGIN))
    roe = info.get("returnOnEquity")
    checks.append(("low ROE",
                   None if roe is None else roe >= QUALITY_MIN_ROE))
    de = info.get("debtToEquity")
    checks.append(("high debt",
                   None if de is None else de <= QUALITY_MAX_DE))
    if margin is None and roe is None and de is None:
        # No real fundamental data came back (bad/missing quote) — grade as
        # n/a instead of failing the dividend check on absent data.
        return Fundamentals(mcap=mcap)
    div = info.get("dividendYield")
    checks.append(("no dividend", bool(div and div > 0)))

    evaluated = [(label, ok) for label, ok in checks if ok is not None]
    passed = sum(1 for _, ok in evaluated if ok)
    fails = [label for label, ok in evaluated if not ok]
    if not evaluated:
        return Fundamentals(mcap=mcap)
    return Fundamentals(
        mcap=mcap,
        quality=f"{passed}/{len(evaluated)}",
        fails=fails,
        strong=len(fails) <= 1 and passed >= 2,
    )


def fetch_fundamentals(tickers: list[str]) -> dict[str, Fundamentals]:
    """
    Fetch market cap + quality grade for the given tickers concurrently.
    Every requested ticker gets an entry; fetch failures come back as a
    default Fundamentals (mcap None, quality "n/a") so the caller can
    fail-open on the market-cap filter.
    """
    out: dict[str, Fundamentals] = {}
    if not tickers:
        return out
    print(f"Fetching fundamentals for {len(tickers)} candidate tickers...",
          flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fundamentals, t): t for t in tickers}
        for fut in as_completed(futures):
            out[futures[fut]] = fut.result()
    return out


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

GREEN = "#1a7f37"   # support
RED = "#cf222e"     # resistance


GOLD = "#9a6700"    # fundamentally strong badge
PURPLE = "#8250df"  # confluence section


def _quality_cell(h: Hit) -> str:
    if h.quality == "n/a":
        return '<span style="color:#999;">n/a</span>'
    if h.strong:
        detail = (f'<br><span style="color:#999;font-size:11px;">'
                  f'{", ".join(h.quality_fails)}</span>' if h.quality_fails else "")
        return (f'<span style="color:{GOLD};font-weight:bold;">'
                f'&#9733; Strong {h.quality}</span>{detail}')
    return (f'{h.quality}<br><span style="color:#999;font-size:11px;">'
            f'{", ".join(h.quality_fails)}</span>')


def _rs_values(vals: dict[str, float]) -> str:
    """Render '+1.2% 1M · +4.5% 3M', each number colored by its own sign."""
    parts = []
    for label in RS_LOOKBACKS:
        v = vals.get(label)
        if v is None:
            parts.append(f'<span style="color:#999;">{label} n/a</span>')
        else:
            color = GREEN if v >= 0 else RED
            parts.append(f'<span style="color:{color};font-weight:bold;">'
                         f'{v:+.1f}%</span> '
                         f'<span style="color:#999;">{label}</span>')
    return " &middot; ".join(parts)


def _rs_cell(h: Hit) -> str:
    # Sect/SPY lives in the sector heading (same for every row in the block);
    # the row keeps the two stock-specific legs.
    lines = [
        f'<span style="color:#666;">Stk/Sect</span> {_rs_values(h.rs_stock_sector)}',
        f'<span style="color:#666;">Stk/SPY</span> {_rs_values(h.rs_stock_spy)}',
    ]
    return ('<span style="font-size:12px;white-space:nowrap;">'
            + "<br>".join(lines) + "</span>")


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
        f'<td style="padding:8px;border-bottom:1px solid #eee;">{_quality_cell(h)}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;">{_rs_cell(h)}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee;">{when}<br>'
        f'<span style="color:#999;font-size:12px;">{h.candle_date}</span></td>'
        "</tr>"
    )


def _sector_block(sector: str, hits: list[Hit]) -> str:
    # support first (green), then resistance (red); within each, fundamentally
    # strong names first, then most-recent first
    hits = sorted(hits, key=lambda h: (0 if h.kind == "support" else 1,
                                       not h.strong, h.weeks_ago, h.ticker))
    s_ct = sum(1 for h in hits if h.kind == "support")
    r_ct = sum(1 for h in hits if h.kind == "resistance")
    rows = "\n".join(_hit_row(h) for h in hits)
    # sector-vs-SPY relative strength is per-sector, so it belongs up here
    sect_rs = next((h.rs_sector_spy for h in hits if h.rs_sector_spy), None)
    if sect_rs is None:
        rs_badge = ""
    else:
        rs_badge = (f' <span style="font-size:13px;font-weight:normal;'
                    f'color:#666;">vs SPY</span> <span style="font-size:14px;'
                    f'font-weight:normal;">{_rs_values(sect_rs)}</span>')
    return f"""
        <h2 style="margin-top:30px;margin-bottom:6px;border-bottom:2px solid #ddd;
                   padding-bottom:4px;font-size:18px;">{sector}{rs_badge}
          <span style="font-size:13px;font-weight:normal;">
            &nbsp;<span style="color:{GREEN};">{s_ct} support</span> &middot;
            <span style="color:{RED};">{r_ct} resistance</span></span></h2>
        <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;">
          <tr style="background:#f4f4f4;color:#555;text-align:left;">
            <th style="padding:8px;">Ticker</th>
            <th style="padding:8px;">Signal</th>
            <th style="padding:8px;">Last Close</th>
            <th style="padding:8px;">Level(s)</th>
            <th style="padding:8px;">Quality</th>
            <th style="padding:8px;">Rel Strength</th>
            <th style="padding:8px;">Touched</th>
          </tr>
          {rows}
        </table>"""


def _confluence_section(confluence: list[Hit]) -> str:
    """
    Trailing section listing stocks whose support/resistance fired on BOTH
    the EMA cloud and an AVWAP (raw rules, no structure filter).
    """
    heading = (f'<h2 style="margin-top:36px;margin-bottom:6px;'
               f'border-bottom:2px solid {PURPLE};padding-bottom:4px;'
               f'font-size:18px;color:{PURPLE};">&#9889; Confluence '
               f'<span style="font-size:13px;font-weight:normal;color:#666;">'
               f'EMA cloud + AVWAP on the same side</span></h2>')
    if not confluence:
        return (heading
                + '<p style="color:#888;margin-top:8px;">No confluence setups today.</p>')
    hits = sorted(confluence, key=lambda h: (0 if h.kind == "support" else 1,
                                             not h.strong, h.weeks_ago, h.ticker))
    s_ct = sum(1 for h in hits if h.kind == "support")
    r_ct = sum(1 for h in hits if h.kind == "resistance")
    rows = "\n".join(_hit_row(h) for h in hits)
    return f"""
        {heading}
        <p style="margin:4px 0 8px;font-size:13px;">
          <span style="color:{GREEN};">{s_ct} support</span> &middot;
          <span style="color:{RED};">{r_ct} resistance</span></p>
        <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;">
          <tr style="background:#f4f4f4;color:#555;text-align:left;">
            <th style="padding:8px;">Ticker</th>
            <th style="padding:8px;">Signal</th>
            <th style="padding:8px;">Last Close</th>
            <th style="padding:8px;">Level(s)</th>
            <th style="padding:8px;">Quality</th>
            <th style="padding:8px;">Rel Strength</th>
            <th style="padding:8px;">Touched</th>
          </tr>
          {rows}
        </table>"""


def build_email_html(support: list[Hit], resistance: list[Hit],
                     confluence: list[Hit], scanned: int) -> str:
    date_str = datetime.now(timezone.utc).astimezone().strftime("%A, %B %d, %Y")
    strong_ct = sum(1 for h in support + resistance if h.strong)

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
        <b style="color:{RED};">&#9632; {len(resistance)} resistance</b> &nbsp;&middot;&nbsp;
        <b style="color:{GOLD};">&#9733; {strong_ct} fundamentally strong</b> &nbsp;&middot;&nbsp;
        <b style="color:{PURPLE};">&#9889; {len(confluence)} confluence</b>
        &nbsp;&middot;&nbsp;<span style="color:#888;">grouped by GICS sector</span></p>
      {blocks}
      {_confluence_section(confluence)}
      <p style="color:#999;font-size:12px;margin-top:30px;">
        AVWAP support/resistance = weekly wick within {TOLERANCE*100:.1f}% of the AVWAP
        and close on the supporting/resisting side.
        EMA-cloud = strict rejection: the wick pulls into the EMA9&ndash;EMA20 zone and the
        candle closes back beyond it with the close in the outer third of the range.
        Structure filter: cloud signals require both AVWAPs on the far side of the 9 EMA;
        AVWAP signals require the 9 EMA on the far side of both AVWAPs.
        Checked over the last {LOOKBACK_WEEKS} weekly candles.
        &#9889; Confluence = the same signal fired on BOTH the EMA cloud and at least one
        AVWAP within the lookback (raw touch/rejection rules, structure filter not
        applied &mdash; an AVWAP sitting in or near the cloud is what forms the zone).
        Quality = fundamental checks passed / evaluable: net margin &ge; {QUALITY_MIN_MARGIN:.0%},
        ROE &ge; {QUALITY_MIN_ROE:.0%}, debt/equity &le; {QUALITY_MAX_DE:.0f}%, pays a dividend
        (checks with unavailable data, e.g. debt/equity for banks, are excluded).
        &#9733; Strong = at most one failed check and two or more passed &mdash; wheel-suitable
        (CSP at green support levels, CC at red resistance levels).
        Rel Strength = % return difference on daily closes over 1-month (21
        trading days) and 3-month (63 trading days) lookbacks.
        The sector heading shows the sector's GICS ETF vs SPY; each row shows
        Stk/Sect = the stock vs its sector ETF and Stk/SPY = the stock vs SPY.
        All legs green = leading stock in a leading sector (tailwind for CSPs);
        all red = laggard in a lagging sector. 1M green with 3M red flags an
        early turnaround; 1M red with 3M green flags fading momentum.
        Automated screener &middot; not financial advice.</p>
    </body></html>"""


OUTPUT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "screener_output.html")


def save_html(html: str) -> None:
    """Write the report next to the script so local runs can open it."""
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report saved to {OUTPUT_HTML}", flush=True)


def send_email(html: str, support: list[Hit], resistance: list[Hit],
               confluence: list[Hit]) -> None:
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_APP_PASSWORD")
    if not user or not password:
        print("EMAIL_USER / EMAIL_APP_PASSWORD not set - skipping send "
              f"(open {OUTPUT_HTML} to view the report).", flush=True)
        return

    msg = MIMEMultipart("alternative")
    today = datetime.now(timezone.utc).astimezone().strftime("%b %d")
    msg["Subject"] = (f"S&P500 Weekly S/R - {today}: "
                      f"{len(support)} support, {len(resistance)} resistance, "
                      f"{len(confluence)} confluence")
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
    confluence: list[Hit] = []
    for t, df in data.items():
        try:
            s, r, cs, cr = evaluate(t, df)
        except Exception as exc:  # noqa: BLE001
            print(f"  {t}: eval error {exc}", flush=True)
            continue
        sector = universe.get(t, "Unknown")
        for hit, bucket in ((s, support), (r, resistance),
                            (cs, confluence), (cr, confluence)):
            if hit:
                hit.sector = sector
                bucket.append(hit)

    print(f"Support hits: {len(support)} | Resistance hits: {len(resistance)} "
          f"| Confluence hits: {len(confluence)} (price >= ${MIN_PRICE:g})",
          flush=True)

    # Fundamentals (market cap + quality grade): only fetched for stocks that
    # actually produced a hit.
    hit_tickers = {h.ticker for h in support + resistance + confluence}
    funds = fetch_fundamentals(sorted(hit_tickers))

    def passes_mcap(h: Hit) -> bool:
        # Fail-open: keep a stock if its market cap could not be retrieved,
        # rather than dropping a valid signal on a transient data error.
        f = funds.get(h.ticker)
        return f is None or f.mcap is None or f.mcap >= MIN_MARKET_CAP

    dropped = sum(1 for h in support + resistance
                  if funds.get(h.ticker) is not None
                  and funds[h.ticker].mcap is not None
                  and funds[h.ticker].mcap < MIN_MARKET_CAP)
    unknown = sum(1 for t in hit_tickers
                  if funds.get(t) is None or funds[t].mcap is None)
    support = [h for h in support if passes_mcap(h)]
    resistance = [h for h in resistance if passes_mcap(h)]
    confluence = [h for h in confluence if passes_mcap(h)]

    for h in support + resistance + confluence:
        f = funds.get(h.ticker)
        if f is not None:
            h.quality, h.quality_fails, h.strong = f.quality, f.fails, f.strong

    # Relative strength vs SPY and sector ETFs (1M / 3M on daily closes).
    remaining = {h.ticker for h in support + resistance + confluence}
    rs_tickers = sorted({BENCHMARK, *SECTOR_ETF.values(), *remaining})
    print(f"Downloading daily closes for relative strength "
          f"({len(rs_tickers)} tickers)...", flush=True)
    try:
        closes = download_daily_closes(rs_tickers)
        annotate_relative_strength(support + resistance + confluence, closes)
    except Exception as exc:  # noqa: BLE001
        print(f"  relative-strength download failed: {exc} (legs left n/a)",
              flush=True)

    strong_ct = sum(1 for h in support + resistance if h.strong)
    print(f"Market-cap filter (>= ${MIN_MARKET_CAP/1e9:g}B): dropped {dropped}, "
          f"{unknown} unknown (kept). "
          f"Final: {len(support)} support | {len(resistance)} resistance "
          f"| {strong_ct} fundamentally strong.",
          flush=True)

    conf_sup = sorted(h.ticker for h in confluence if h.kind == "support")
    conf_res = sorted(h.ticker for h in confluence if h.kind == "resistance")
    print(f"Confluence (EMA cloud + AVWAP, same side): "
          f"{len(conf_sup)} support {conf_sup or '[]'} | "
          f"{len(conf_res)} resistance {conf_res or '[]'}", flush=True)

    html = build_email_html(support, resistance, confluence, len(data))
    save_html(html)
    send_email(html, support, resistance, confluence)
    return 0


if __name__ == "__main__":
    sys.exit(main())
