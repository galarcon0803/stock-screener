# Stock Sentiment Tracker

Scrapes Reddit for stock mentions, pairs them with live market data, scores each
ticker with a **conviction model**, stores history in SQLite, and emails a daily
HTML report. Designed to run unattended on GitHub Actions.

> **Not investment advice.** Reddit sentiment is noisy and frequently wrong.
> This is a research/automation toy. Do your own due diligence.

---

## Reddit Data API usage (read-only)

This project uses the Reddit Data API strictly for **read-only, non-commercial,
personal research**, and is a deliberately minimal-footprint API consumer:

- **Read-only.** It only issues read (GET) requests. It performs **no write
  operations of any kind** — it never posts, comments, votes, messages users,
  flairs, or moderates. It adds zero spam or interaction load to any community.
- **Low frequency.** It runs as a scheduled job **once per day** and stays well
  within the OAuth rate limit (100 QPM).
- **What it reads.** On each daily run it reads recent public posts (hot / new /
  top, plus controversial for one subreddit) and their top-level comments from a
  fixed list of finance/investing subreddits, extracts stock-ticker mentions
  (e.g. `$NVDA`, `AMD`), and records only lightweight aggregate metadata per
  mention (subreddit, post ID, score, upvote ratio, comment count, and a short
  text snippet).
- **What it does with it.** Mentions are aggregated to gauge which tickers are
  being discussed and how, paired with **external** stock-market price/volume
  data (from a financial-data provider, not Reddit) and a sentiment
  classification step, then summarized into a **private daily email report** for
  the author. No individual users' content is republished or redistributed
  publicly. No personal data is collected, profiled, or shared.

**Why the Data API and not Devvit:** the tool needs to read ~14 large public
finance subreddits the author does not own or moderate (so a per-subreddit Devvit
install is not possible), and it relies on an off-platform Python stack (pandas,
an external market-data provider, a third-party LLM API for sentiment, and email
delivery) that does not fit Devvit's in-platform TypeScript runtime and outbound
`fetch` allowlist.

**Subreddits used:** r/wallstreetbets, r/stocks, r/stockmarket, r/smallstreetbets,
r/options, r/optionstrading, r/thetagang, r/daytrading, r/undervaluedstonks,
r/TheRaceTo10Million, r/biotech_stocks, r/stockstobuytoday, r/wallstreetbetsnew,
r/optionsmillionaire. *(Authoritative list lives in [`config.py`](config.py).)*

---

## How it works

```
Reddit (PRAW)  ──▶  ticker extraction + validation (yfinance, cached)
                         │
Market data (yfinance) ──┤
                         ▼
Sentiment (Claude API)  ──▶  scorer  ──▶  SQLite  ──▶  HTML report  ──▶  email
```

Each ticker gets a **conviction score (0–100)** = `signal*0.45 + opportunity*0.40 + trend*0.15`,
then a tier: **ACT** (80+), **WATCH** (60–79), **MONITOR** (40–59), **NOISE** (<40).

- **Signal** — mention velocity vs 7-day baseline, engagement-weighted sentiment
  quality (DD > general > FOMO, bearish subtracts), discussion depth, subreddit weight.
- **Opportunity** — price-vs-signal lag (reward signal *ahead* of price), volume
  expansion, small-float/high-short squeeze potential, minus risk flags
  (imminent earnings, already-ran-up, missing data).
- **Trend** — history-aware: rewards tickers whose past signals paid off (via the
  `ticker_outcomes` backfill), neutral (50) for first-timers.

All weights, subreddits, thresholds, and the Claude model live in
[`config.py`](config.py) and are meant to be tuned as you see results.

---

## Project layout

```
stock-sentiment-tracker/
├── .github/workflows/daily_report.yml   # cron + manual trigger
├── config.py                            # all constants / weights / secrets access
├── main.py                              # pipeline orchestrator (has CLI flags)
├── src/
│   ├── database.py            # SQLite schema + all reads/writes (network-free)
│   ├── reddit_scraper.py      # PRAW scraping + ticker extraction/validation
│   ├── stock_fetcher.py       # yfinance market data (retries, caching)
│   ├── sentiment_analyzer.py  # Claude batched classification (prompt-cached)
│   ├── scorer.py              # conviction model
│   ├── report_generator.py    # Jinja2 -> HTML
│   └── email_sender.py        # Gmail SMTP or SendGrid
├── templates/report.html      # dark, mobile-friendly email template
├── tests/test_offline.py      # no-network smoke test
└── data/sentiment.db          # created on first run; persisted in CI
```

---

## Setup

### 1. Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure secrets

Copy `.env.example` to `.env` and fill it in:

| Variable | Where to get it |
|---|---|
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | reddit.com/prefs/apps → create a **script** app |
| `REDDIT_USER_AGENT` | any string, e.g. `StockTracker/1.0 by u/you` |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `EMAIL_PROVIDER` | `gmail` or `sendgrid` |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | Google Account → Security → App Passwords |
| `SENDGRID_API_KEY` / `SENDGRID_FROM_EMAIL` | only if using SendGrid |
| `REPORT_EMAIL_TO` | recipient(s), comma-separated |

### 3. Run

```powershell
python main.py --dry-run        # full pipeline, writes out/report_<date>.html, no email
python main.py --limit 10       # only score the 10 most-mentioned tickers
python main.py                  # full run + email
```

Useful flags: `--no-email`, `--no-outcomes` (skip return backfill),
`--no-sentiment` (skip Claude — labels default to neutral, for offline tests).

### 4. Offline test (no API keys needed)

```powershell
python tests/test_offline.py
```

---

## Deploying to GitHub Actions

1. Push the repo to GitHub.
2. Add each variable above as a **repository secret**
   (Settings → Secrets and variables → Actions).
3. The workflow runs weekdays at 13:30 UTC and on manual dispatch. It commits the
   updated `data/sentiment.db` back to the repo so history accumulates run-to-run
   (a cache restore is the fallback).

### Cron / timezone note
Cron is UTC and does not follow US daylight-saving. `30 13 * * 1-5` is 9:30 AM
EDT; during EST (winter) switch to `30 14 * * 1-5` for the same local time.

### Database persistence
SQLite committed to the repo is fine for a long time. If it grows large or commit
churn becomes annoying, migrate `database.py` to a free Postgres (e.g. Supabase) —
the schema is unchanged; swap `sqlite3` for `psycopg2`.

---

## Tuning notes

- The first several runs have **no trend history** — trend bonus stays at the
  neutral 50 until `ticker_outcomes` accumulates and 5d/30d returns backfill.
- Expand `TICKER_BLACKLIST` in `config.py` as false positives appear in reports.
- For lower cost, set `CLAUDE_MODEL=claude-haiku-4-5-20251001` (env or config) —
  cheaper classification at some loss of DD-vs-FOMO nuance.
- Adjust `MIN_MENTIONS_TO_SCORE`, `POSTS_PER_SUBREDDIT`, and `LOOKBACK_HOURS` to
  trade coverage against API/runtime cost.
