# S&P 500 Weekly Support/Resistance Screener

A GitHub Actions screener that scans all S&P 500 stocks every weekday at **6 PM EST**
and emails the matches to `avin.khurana18@gmail.com`.

## What it looks for

On **weekly candles**, for every S&P 500 stock, it computes:

- **Anchored VWAP (AVWAP)** — two of them:
  - anchored at the first weekly candle of the **current year**
  - anchored at the first weekly candle of the **previous year**
- **EMA 9** and **EMA 20** — the zone between them is the **"EMA cloud"**

Then, over the **2 most recent weekly candles** (current + last 1), it flags support
and resistance. The two level types use different rules:

**AVWAP levels** (`AVWAP current year`, `AVWAP previous year`) — proximity:

| Signal | Rule |
| --- | --- |
| **Support** | A weekly **low** wicks within **1%** of the AVWAP **and** the candle **closes above** it. |
| **Resistance** | A weekly **high** wicks within **1%** of the AVWAP **and** the candle **closes below** it. |

**EMA cloud** (the zone between EMA9 and EMA20) — strict rejection (kills consolidation noise):

| Signal | Rule |
| --- | --- |
| **Support** | The **low** pulls **into** the cloud zone (without blowing through the bottom), the candle **closes back above** the whole cloud, and the close is in the **upper third** of the range. |
| **Resistance** | The **high** pushes **into** the cloud zone (without breaking above the top), the candle **closes back below** the whole cloud, and the close is in the **lower third** of the range. |

### Trend-structure confirmation

On top of the touch rules, each signal must pass a structure check confirming the
triggering level is the *nearest* of the two structures (with the other one stacked
beyond it). The ordering is measured on the **most recent weekly candle** and applied
**per level** — a level that fails is dropped, but a stock can still qualify through a
different level that passes.

| Signal source | Required ordering |
| --- | --- |
| **EMA-cloud support** | **Both** AVWAPs below the 9 EMA |
| **EMA-cloud resistance** | **Both** AVWAPs above the 9 EMA |
| **AVWAP support** | 9 EMA below **both** AVWAPs |
| **AVWAP resistance** | 9 EMA above **both** AVWAPs |

### Eligibility filters

A stock is only considered if it passes **both** of these:

| Filter | Threshold |
| --- | --- |
| **Price** | Last weekly close **> $50** |
| **Market cap** | **> $10 billion** |

The price filter is applied up front (from the weekly data). Market cap is fetched
(via yfinance) only for stocks that already produced a support/resistance hit, to
avoid pulling 500 quotes. If a stock's market cap can't be retrieved (transient data
error), it is **kept** rather than dropped, so a valid signal isn't lost to an API hiccup.

### Fundamental quality grade (wheel suitability)

Every stock that produces a hit is also graded on four fundamental checks
(fetched via yfinance, only for hit tickers):

| Check | Threshold |
| --- | --- |
| **Net profit margin** | ≥ 5% |
| **Return on equity** | ≥ 10% |
| **Debt / equity** | ≤ 200% |
| **Dividend** | pays one |

A check whose data isn't available (e.g. debt/equity for banks, ROE for
negative-equity companies like MCD) is **excluded**, not failed. The email shows
the score as `passed/evaluable` (e.g. `3/4`) with the failed checks spelled out,
and a stock earns a gold **★ Strong** badge when it fails **at most one** check
and passes **at least two**. Strong names sort to the top of each sector block —
these are the wheel candidates: sell **cash-secured puts** at green support
levels, **covered calls** at red resistance levels, on businesses you'd be
comfortable owning if assigned.

### Relative strength (1M / 3M)

Each hit also gets a three-leg relative-strength readout, measured as the
**% return difference on daily closes** over two lookbacks — **1 month
(21 trading days)** and **3 months (63 trading days)** — using the SPDR sector
ETF (XLK, XLV, XLF, ...) as the sector proxy:

| Leg | Shown | Meaning |
| --- | --- | --- |
| **Sector vs SPY** | in the **sector heading** | is the *sector* leading the market? |
| **Stk/Sect** | per row | the stock vs its sector ETF — is the *stock* leading its sector? |
| **Stk/SPY** | per row | the stock vs SPY — net result of the two above |

All three legs share the same calculation, so they're internally consistent:
`Stk/SPY ≈ Sector-vs-SPY + Stk/Sect` at each horizon.

Green = outperforming, red = lagging, at each horizon independently. For the
wheel: **all legs green at a support level** = a leading stock in a leading
sector pulling back to support — the highest-conviction CSP setup. All red = a
laggard in a lagging sector; support there breaks more often (falling-knife
risk). The two horizons add a rotation read: **1M green with 3M red** flags an
early turnaround; **1M red with 3M green** flags fading momentum. A green
sector with a red Stk/Sect can flag a catch-up candidate, but demands the
quality grade be strong.

The email is **grouped by GICS sector**. Under each sector heading, matching stocks
are listed with **support in green** and **resistance in red**, showing the ticker,
signal, last close, which level(s) triggered, its quality grade, its relative
strength, and how recent the touch was. Each sector header also shows its
support/resistance counts, and the top of the email carries the overall totals plus
the count of fundamentally strong names.

## When to sell CSPs / CCs — and when to stay away

The screener finds *where* (levels) and *what* (quality grade); this is the
*when*. Rules of thumb for the wheel:

**Green light for cash-secured puts:**

- **VIX 18–28** — elevated fear means rich premiums, but not a crash in
  progress. This is the sweet spot.
- **SPY above a rising 200-day / 40-week moving average** — you're selling puts
  into a dip within an uptrend, not into a downtrend.
- The stock shows up in this screener at **tested weekly support** — strike the
  put at or below that level.
- **No earnings before expiry** (or size down if there is — an earnings gap
  through your strike is the #1 way to get assigned at the worst price).
- The stock's own **IV rank > 30** — you're being paid above its average.

**Stay away from CSPs when:**

- **VIX term structure inverts** (spot VIX above 3-month VIX futures, or
  VIX > ~35 and still rising): that's a crash unfolding, not a dip. Premiums
  look juicy precisely because the market expects more downside. Selling puts
  *after* the VIX spike peaks and rolls over is historically a great trade;
  selling *into* the spike is historically a ruinous one.
- **SPY below a falling 200-day MA** — in a 2022-style downtrend every support
  level fails for months.
- **VIX < 13** — premiums are too thin to compensate tail risk.
- A **big macro event** (FOMC, hot-streak CPI) lands inside the expiry window
  and you haven't sized for it.

**For covered calls (after assignment):**

- Sell CCs when the stock approaches a **resistance level flagged here**, and
  never strike below your cost basis in the first months after assignment.
- **Don't sell CCs right off a crash bottom** — that's how a COVID-style
  assignment becomes a capped 5% recovery while the stock runs 80%. After a
  market-wide (not company-specific) crash, hold uncovered or sell far-OTM.
- If the stock gapped down on **company-specific** bad news, re-check the
  quality grade — the wheel's rule #1 is never wheel a stock you wouldn't
  own outright.

## Setup

The screener sends email through **Gmail SMTP**, which needs a Google **App Password**
(not your normal password).

### 1. Create a Gmail App Password

1. Enable 2-Step Verification on your Google account: <https://myaccount.google.com/security>
2. Go to <https://myaccount.google.com/apppasswords>
3. Create an app password (name it e.g. "screener") and copy the 16-character code.

### 2. Add GitHub repository secrets

In your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**,
add:

| Secret name | Value |
| --- | --- |
| `EMAIL_USER` | your Gmail address (e.g. `avin.khurana18@gmail.com`) |
| `EMAIL_APP_PASSWORD` | the 16-character app password (no spaces) |

### 3. Push and enable

```bash
git add .
git commit -m "Add S&P 500 weekly S/R screener"
git push
```

Then open the **Actions** tab. The job runs on schedule; you can also trigger it
manually with **Run workflow** (thanks to `workflow_dispatch`).

## Schedule / timezone note

GitHub cron runs in **UTC** and does **not** observe daylight saving. The workflow is set to
`0 23 * * 1-5` = **6:00 PM EST** (and 7:00 PM EDT during summer). The market closes at
4:00 PM ET year-round, so the scan always runs after the close. To lock it to exactly
6 PM ET during summer, change the cron to `0 22 * * 1-5`.

## Run locally

```bash
pip install -r requirements.txt

# Every run writes the report to screener_output.html next to the script.
# Without email creds it skips sending — just open the file:
python screener.py
open screener_output.html

# To also send the email:
EMAIL_USER="you@gmail.com" EMAIL_APP_PASSWORD="xxxx" python screener.py
```

## Wheel backtest

`wheel_backtest.py` is a standalone 10-year simulation of the classic wheel
(monthly ~30-delta cash-secured puts → assignment → covered calls above cost
basis) on a fixed basket of quality mega-caps, with $50k starting capital.
Option premiums are modeled with **Black-Scholes on 63-day realized volatility
× 1.15 IV markup** (real decade-long option-chain history is paywalled), with
a 5% fill haircut, per-contract commissions, dividends while holding, and
T-bill interest on idle cash.

```bash
python wheel_backtest.py
```

Reference result (2016-07 → 2026-06): **10.8% CAGR with a −11.7% max drawdown
and 8.5% volatility**, vs SPY buy-and-hold at 15.1% CAGR / −33.7% / 18.0% —
i.e. roughly two-thirds of the market's return with one-third of the drawdown,
and positive years in 2018 and 2022 when SPY was down. Simulated premiums, not
real chains; treat the shape as the finding, not the exact numbers.

## Tuning

Edit the constants at the top of `screener.py`:

- `TOLERANCE` — AVWAP touch band (default `0.01` = 1%)
- `LOOKBACK_WEEKS` — how many recent weekly candles to inspect (default `2`)
- `REJECT_FRACTION` — EMA-cloud rejection strength; close must be in this outer
  fraction of the candle range (default `2/3` = outer third)
- `MIN_PRICE` — minimum last close (default `50.0`)
- `MIN_MARKET_CAP` — minimum market cap in dollars (default `10_000_000_000` = $10B)
- `EMA_FAST` / `EMA_SLOW` — EMA cloud periods (default 9 / 20)
- `QUALITY_MIN_MARGIN` / `QUALITY_MIN_ROE` / `QUALITY_MAX_DE` — thresholds for
  the fundamental quality grade (defaults 5% / 10% / 200%)
- `RS_LOOKBACKS` — relative-strength horizons in trading days (default `{"1M": 21, "3M": 63}`)

Not financial advice — automated screening only.
