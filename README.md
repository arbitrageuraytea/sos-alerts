# SoS Alerts — Start of Swing scanner

Automated scanner for "Start of Swing" breakouts on US stocks. Runs entirely on
GitHub Actions (free) and pings you on Telegram.

## What it scans for

**Premarket (08:45 ET):** filters all US-listed common stocks down to a daily
candidate list using the **2LYNCH** framework on daily candles:

| | Rule | Threshold |
|---|---|---|
| 2 | Tight days pre-breakout | ≥2 of last 5 days with `|chg| ≤ 1.5%` |
| L | Linearity of prior trend | R² of last 20 closes ≥ 0.7 |
| Y | Young trend | ≤3 prior breakouts above 20-day high in last 60 days |
| N | Narrow / red day pre-breakout | prior day range ≤ 0.5×ATR(20) OR red |
| C | Shallow consolidation | 5–10 day window, depth ≤ 15%, ≤1 day with `chg ≤ −4%` |

Plus: soft preference for Tech / Healthcare / Consumer Cyclical / Communication
sectors; Lynch-style fundamentals score (PEG<1, earnings growth >20%, low D/E,
profitable); theme tagging (AI, Quantum, Semis, Space, Defense, Nuclear, …) from
the company business summary.

**Intraday (every 5 min, 9:30–16:00 ET):** for each eligible ticker checks:

- price up ≥ 4% from prior close
- close in top 25% of day's range (the "75% of high" rule, **H**)
- cumulative volume ≥ 6M → 🟡 **EARLY** alert
- cumulative volume ≥ 8.9M → 🟢 **CONFIRMED** alert
- gap-up flag (open ≥ +2%) → ⚠️ tagged in alert as fade risk

Each ticker fires at most one EARLY and one CONFIRMED alert per day.

## Setup (one-time)

### 1. Create the GitHub repo

```bash
cd /Users/ahmedtariq/sos-alerts
git init -b main
git add .
git commit -m "initial commit"
gh repo create arbitrageuraytea/sos-alerts --public --source=. --push
```

(If you don't have `gh` installed: `brew install gh && gh auth login`, or create
the repo on github.com and add it as a remote manually.)

**Make the repo public** so GitHub Actions minutes are free + unlimited.
Your bot token is stored as a Secret, not in the repo.

### 2. Add Telegram secrets to the repo

```bash
gh secret set TELEGRAM_BOT_TOKEN --body "8655479749:AAFZYpE5mD_Ur-AVSD2g5qbEM1Ypka-keUQ"
gh secret set TELEGRAM_CHAT_ID   --body "5816219660"
```

Or via the website: Repo → Settings → Secrets and variables → Actions → New
repository secret.

### 3. Test it

In the GitHub UI: Actions tab → "Premarket Filter" → "Run workflow" → set
`limit` to e.g. `500` for a 2-min smoke test. Then "Intraday Scan" → "Run
workflow". Check the run logs and your Telegram.

For a full premarket run, leave `limit` at 0 (default). Expect ~10–20 min for
~7000 tickers.

## Local testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Quick smoke test on 200 tickers
LIMIT=200 python premarket_filter.py

# To send a Telegram from your machine:
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python intraday_scan.py
```

## Files

```
premarket_filter.py     2LYNCH daily-chart filter (writes state/eligible.json)
intraday_scan.py        5-min intraday scanner (sends Telegram alerts)
telegram_alert.py       Telegram sender helper
tickers.py              US ticker universe from NASDAQ Trader
themes.py               Theme keyword dict (edit to add/remove themes)
.github/workflows/      GitHub Actions cron jobs
state/eligible.json     Today's eligible candidates (rebuilt each morning)
state/sent_today.json   Per-day de-dupe log
```

## Tuning

Common knobs to adjust in `premarket_filter.py`:

- `PREFERRED_SECTORS` — add/remove sectors
- `passes_2lynch()` — every threshold lives here
- in `themes.py` — keyword dict for theme tagging

In `intraday_scan.py`:

- `EARLY_VOL` / `CONFIRMED_VOL` — volume thresholds
- `UP_PCT` — % up trigger
- `TOP_OF_RANGE` — H rule (0.75 = top 25%)
- `GAP_UP_PCT` — gap-up flag threshold

## Caveats

- yfinance intraday is **~15 min delayed**. For "first few hours after open"
  this is fine — 6M/8.9M cumulative-volume triggers fire well within window.
  Upgrade path to true real-time: Alpaca paid SIP (~$99/mo) or Polygon paid.
- GHA scheduled crons can be **delayed 5–15 min** under load. Not a blocker.
- If yfinance rate-limits a chunk, those tickers silently get skipped that
  cycle — they'll pick up on the next 5-min run. The premarket filter is
  more critical; a single failed batch costs at most 200 tickers for the day.
- yfinance `.info` is sometimes empty for thinly traded names → those get
  scored 0 on Lynch and have no theme tags but still pass technicals.

## Rotating the Telegram bot token

If the token leaks: Telegram → @BotFather → `/revoke` → choose your bot →
new token printed. Update the GitHub Secret `TELEGRAM_BOT_TOKEN`.
