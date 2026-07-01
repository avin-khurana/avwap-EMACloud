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

The email has **separate Support and Resistance sections**, each listing the
ticker, last close, which level(s) triggered, and how recent the touch was.

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

# Without email creds it prints the HTML to stdout instead of sending:
python screener.py

# To actually send:
EMAIL_USER="you@gmail.com" EMAIL_APP_PASSWORD="xxxx" python screener.py
```

## Tuning

Edit the constants at the top of `screener.py`:

- `TOLERANCE` — AVWAP touch band (default `0.01` = 1%)
- `LOOKBACK_WEEKS` — how many recent weekly candles to inspect (default `2`)
- `REJECT_FRACTION` — EMA-cloud rejection strength; close must be in this outer
  fraction of the candle range (default `2/3` = outer third)
- `MIN_PRICE` — minimum last close (default `50.0`)
- `MIN_MARKET_CAP` — minimum market cap in dollars (default `10_000_000_000` = $10B)
- `EMA_FAST` / `EMA_SLOW` — EMA cloud periods (default 9 / 20)

Not financial advice — automated screening only.
