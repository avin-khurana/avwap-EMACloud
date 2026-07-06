"""
10-Year Wheel Strategy Backtest (CSP + CC)
==========================================

Simulates the classic wheel on a fixed basket of quality mega-caps:

  * Sell ~30-delta cash-secured puts at the next monthly expiry (~30-45 DTE).
  * If assigned, sell ~30-delta covered calls struck at or above cost basis.
  * Collect dividends while holding shares. Repeat.

Option premiums are modeled with Black-Scholes on 63-day realized volatility
with a 1.15x implied-volatility markup (the variance risk premium options
sellers historically harvest). Prices are split-adjusted but NOT dividend-
adjusted, so assignment mechanics are realistic and dividends are credited
separately from yfinance's dividend history.

Haircuts applied to stay honest:
  * Premium received = 95% of model mid (slippage) minus $0.65/contract.
  * Puts struck down / calls struck up to whole dollars.
  * Idle cash earns the T-bill rate (it would sit in a money market fund).

Benchmark: SPY buy-and-hold total return (dividends reinvested).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

START = "2016-07-01"
END = "2026-07-01"
CAPITAL = 50_000.0
MAX_POSITIONS = 3            # concurrent tickers (CSP or stock), pooled cash
TARGET_DELTA = 0.30
VOL_WINDOW = 63              # trading days of realized vol
IV_MARKUP = 1.15             # IV / realized-vol ratio
SLIPPAGE = 0.95              # fraction of model premium actually received
COMMISSION = 0.65            # per contract
VOL_FLOOR = 0.12
VOL_CAP_SELECT = 0.45        # skip entries when the name's vol is panicking

BASKET = ["AAPL", "MSFT", "KO", "PEP", "JPM", "PG", "MRK", "CSCO", "WMT", "ABBV"]

# Approximate 3M T-bill rate by year (percent) — used for BS pricing and
# interest on idle cash.
RATES = {2016: 0.4, 2017: 1.0, 2018: 2.0, 2019: 2.3, 2020: 0.4, 2021: 0.05,
         2022: 2.0, 2023: 5.0, 2024: 5.2, 2025: 4.3, 2026: 3.9}


def rf(date: pd.Timestamp) -> float:
    return RATES.get(date.year, 2.0) / 100.0


# --------------------------------------------------------------------------- #
# Black-Scholes helpers
# --------------------------------------------------------------------------- #

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


N_INV_70 = 0.5244005127080407   # Phi^-1(0.70), for 30-delta strikes


def strike_for_delta(s: float, sigma: float, t: float, r: float, put: bool) -> float:
    """Strike whose BS delta magnitude is TARGET_DELTA (0.30)."""
    d1 = N_INV_70 if put else -N_INV_70
    k = s * math.exp((r + 0.5 * sigma ** 2) * t - d1 * sigma * math.sqrt(t))
    return math.floor(k) if put else math.ceil(k)   # conservative rounding


def bs_price(s: float, k: float, sigma: float, t: float, r: float, put: bool) -> float:
    if t <= 0 or sigma <= 0:
        return max(k - s, 0.0) if put else max(s - k, 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if put:
        return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)
    return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

def load_prices(tickers: list[str]) -> tuple[dict[str, pd.DataFrame], pd.DatetimeIndex]:
    """Split-adjusted daily OHLC + dividends per ticker."""
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        h = yf.Ticker(t).history(start=START, end=END, auto_adjust=False,
                                 actions=True)
        if h.empty:
            raise RuntimeError(f"no data for {t}")
        h.index = h.index.tz_localize(None)
        # auto_adjust=False data is split-adjusted already in yfinance history()
        h["logret"] = np.log(h["Close"] / h["Close"].shift(1))
        h["rv"] = h["logret"].rolling(VOL_WINDOW).std() * math.sqrt(252)
        frames[t] = h
    cal = frames[tickers[0]].index
    for t in tickers[1:]:
        cal = cal.union(frames[t].index)
    return frames, cal


def third_fridays(cal: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Monthly expiries: third Friday, rolled back to the prior trading day."""
    out = []
    months = sorted({(d.year, d.month) for d in cal})
    for y, m in months:
        days = pd.date_range(f"{y}-{m:02d}-01", periods=31, freq="D")
        fridays = [d for d in days if d.weekday() == 4 and d.month == m]
        tf = fridays[2]
        candidates = cal[cal <= tf]
        if len(candidates) and candidates[-1] >= cal[0]:
            out.append(candidates[-1])
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #

@dataclass
class ShortPut:
    ticker: str
    strike: float
    premium: float           # per share, net
    expiry: pd.Timestamp


@dataclass
class StockPos:
    ticker: str
    shares: int
    basis: float             # per share, net of premiums received so far
    cc_strike: float | None = None
    cc_premium: float = 0.0
    cc_expiry: pd.Timestamp | None = None


def px(frames, t, date, col="Close") -> float | None:
    df = frames[t]
    if date in df.index:
        v = df.at[date, col]
        return None if pd.isna(v) else float(v)
    prior = df.index[df.index <= date]
    return float(df.at[prior[-1], col]) if len(prior) else None


def vol(frames, t, date) -> float | None:
    df = frames[t]
    idx = df.index[df.index <= date]
    if not len(idx):
        return None
    v = df.at[idx[-1], "rv"]
    return None if pd.isna(v) else max(float(v), VOL_FLOOR)


def run() -> None:
    frames, cal = load_prices(BASKET)
    spy = yf.Ticker("SPY").history(start=START, end=END, auto_adjust=True)
    spy.index = spy.index.tz_localize(None)
    expiries = third_fridays(cal)
    cal = cal[(cal >= expiries[0])]          # start sim at first expiry
    expiries = [e for e in expiries if e >= cal[0]]

    cash = CAPITAL
    puts: dict[str, ShortPut] = {}
    stocks: dict[str, StockPos] = {}
    stats = {"put_cycles": 0, "put_wins": 0, "assignments": 0, "cc_cycles": 0,
             "called_away": 0, "premium": 0.0, "dividends": 0.0, "interest": 0.0}
    equity_curve = []
    exp_set = set(expiries)

    def open_csp(date):
        nonlocal cash
        future = [e for e in expiries if (e - date).days >= 25]
        if not future:
            return
        expiry = future[0]
        t_yrs = max((expiry - date).days, 1) / 365.0
        held = set(puts) | set(stocks)
        cands = []
        for tk in BASKET:
            if tk in held:
                continue
            s, sg = px(frames, tk, date), vol(frames, tk, date)
            if s is None or sg is None or sg > VOL_CAP_SELECT:
                continue
            k = strike_for_delta(s, sg * IV_MARKUP, t_yrs, rf(date), put=True)
            if k * 100 > cash:
                continue
            prem = bs_price(s, k, sg * IV_MARKUP, t_yrs, rf(date), put=True)
            cands.append((prem / k / t_yrs, tk, k, prem))   # annualized yield
        cands.sort(reverse=True)
        while cands and len(puts) + len(stocks) < MAX_POSITIONS:
            _, tk, k, prem = cands.pop(0)
            if k * 100 > cash:
                continue
            net = prem * SLIPPAGE - COMMISSION / 100.0
            cash += net * 100
            stats["premium"] += net * 100
            puts[tk] = ShortPut(tk, k, net, expiry)
            stats["put_cycles"] += 1

    def open_cc(pos: StockPos, date):
        nonlocal cash
        future = [e for e in expiries if (e - date).days >= 25]
        if not future:
            return
        expiry = future[0]
        t_yrs = max((expiry - date).days, 1) / 365.0
        s, sg = px(frames, pos.ticker, date), vol(frames, pos.ticker, date)
        if s is None or sg is None:
            return
        k = strike_for_delta(s, sg * IV_MARKUP, t_yrs, rf(date), put=False)
        k = max(k, math.ceil(pos.basis))     # never cap below cost basis
        prem = bs_price(s, k, sg * IV_MARKUP, t_yrs, rf(date), put=False)
        net = prem * SLIPPAGE - COMMISSION / 100.0
        if net <= 0.05:                       # not worth selling
            return
        cash += net * 100
        stats["premium"] += net * 100
        pos.basis -= net                      # premium lowers effective basis
        pos.cc_strike, pos.cc_premium, pos.cc_expiry = k, net, expiry
        stats["cc_cycles"] += 1

    for date in cal:
        # daily interest on cash
        interest = cash * rf(date) / 252.0
        cash += interest
        stats["interest"] += interest

        # dividends on held shares
        for pos in stocks.values():
            df = frames[pos.ticker]
            if date in df.index:
                div = float(df.at[date, "Dividends"] or 0.0)
                if div > 0:
                    cash += div * pos.shares
                    stats["dividends"] += div * pos.shares

        if date in exp_set:
            # settle short puts
            for tk in list(puts):
                p = puts[tk]
                if p.expiry != date:
                    continue
                s = px(frames, tk, date)
                del puts[tk]
                if s is not None and s < p.strike:
                    cash -= p.strike * 100
                    stocks[tk] = StockPos(tk, 100, p.strike - p.premium)
                    stats["assignments"] += 1
                else:
                    stats["put_wins"] += 1
            # settle covered calls
            for tk in list(stocks):
                pos = stocks[tk]
                if pos.cc_expiry != date:
                    continue
                s = px(frames, tk, date)
                if s is not None and pos.cc_strike is not None and s > pos.cc_strike:
                    cash += pos.cc_strike * 100
                    stats["called_away"] += 1
                    del stocks[tk]
                else:
                    pos.cc_strike, pos.cc_expiry = None, None

            # redeploy
            for pos in stocks.values():
                if pos.cc_strike is None:
                    open_cc(pos, date)
            open_csp(date)

        # mark equity (short options at intrinsic value)
        eq = cash
        for pos in stocks.values():
            s = px(frames, pos.ticker, date) or pos.basis
            eq += pos.shares * s
            if pos.cc_strike is not None:
                eq -= max(s - pos.cc_strike, 0.0) * pos.shares
        for p in puts.values():
            s = px(frames, p.ticker, date)
            if s is not None:
                eq -= max(p.strike - s, 0.0) * 100
        equity_curve.append((date, eq))

    curve = pd.Series(dict(equity_curve)).sort_index()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (curve.iloc[-1] / CAPITAL) ** (1 / years) - 1
    dd = (curve / curve.cummax() - 1).min()
    ann_vol = curve.pct_change().std() * math.sqrt(252)

    spy_curve = spy["Close"] / spy["Close"].iloc[0] * CAPITAL
    spy_curve = spy_curve[spy_curve.index >= curve.index[0]]
    spy_curve = spy_curve / spy_curve.iloc[0] * CAPITAL
    spy_years = (spy_curve.index[-1] - spy_curve.index[0]).days / 365.25
    spy_cagr = (spy_curve.iloc[-1] / CAPITAL) ** (1 / spy_years) - 1
    spy_dd = (spy_curve / spy_curve.cummax() - 1).min()
    spy_vol = spy_curve.pct_change().std() * math.sqrt(252)

    print(f"Period: {curve.index[0].date()} -> {curve.index[-1].date()} "
          f"({years:.1f}y) | capital ${CAPITAL:,.0f}")
    print(f"\n=== WHEEL ({', '.join(BASKET)}) ===")
    print(f"Final value      : ${curve.iloc[-1]:,.0f}")
    print(f"CAGR             : {cagr * 100:.2f}%")
    print(f"Max drawdown     : {dd * 100:.1f}%")
    print(f"Ann. volatility  : {ann_vol * 100:.1f}%")
    print(f"Premium collected: ${stats['premium']:,.0f}")
    print(f"Dividends        : ${stats['dividends']:,.0f}")
    print(f"Cash interest    : ${stats['interest']:,.0f}")
    print(f"CSP cycles       : {stats['put_cycles']} "
          f"({stats['put_wins']} expired worthless, "
          f"{stats['assignments']} assigned -> "
          f"{stats['put_wins'] / max(stats['put_cycles'], 1) * 100:.0f}% win rate)")
    print(f"CC cycles        : {stats['cc_cycles']} "
          f"({stats['called_away']} called away)")
    print(f"\n=== SPY buy & hold (same period) ===")
    print(f"Final value      : ${spy_curve.iloc[-1]:,.0f}")
    print(f"CAGR             : {spy_cagr * 100:.2f}%")
    print(f"Max drawdown     : {spy_dd * 100:.1f}%")
    print(f"Ann. volatility  : {spy_vol * 100:.1f}%")

    # year-by-year
    print("\nYear-by-year wheel return vs SPY:")
    wy = curve.resample("YE").last().pct_change()
    sy = spy_curve.resample("YE").last().pct_change()
    first_year = (curve.resample("YE").last().iloc[0] / CAPITAL) - 1
    wy.iloc[0], sy.iloc[0] = first_year, (spy_curve.resample("YE").last().iloc[0] / CAPITAL) - 1
    for (d, w), (_, s_) in zip(wy.items(), sy.items()):
        print(f"  {d.year}: wheel {w * 100:+6.1f}%   SPY {s_ * 100:+6.1f}%")


if __name__ == "__main__":
    run()
