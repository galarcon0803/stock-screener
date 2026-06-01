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

## Data backends (how Reddit data gets in)

The scraper has a **pluggable backend**, selected by `REDDIT_BACKEND`:

| `REDDIT_BACKEND` | Source | Needs API key? | Runs in CI? |
|---|---|---|---|
| `praw` | Official OAuth Data API | Yes (pending approval) | ✅ Yes |
| `json` *(default)* | Reddit's public `.json` endpoints | No | ❌ **Residential IP only** |

**Why two backends:** Reddit 403-blocks **datacenter IPs** (GitHub Actions, cloud
hosts) at the network level — regardless of method (`.json`, `.rss`, Redlib, full
browser headers all fail from CI; verified empirically). The archive services
(Pushshift/PullPush) are ~a year stale and unusable for a *daily* tracker. So the
only no-API way to get fresh data is to **scrape from a residential IP**.

### No-API path (default, no Reddit key)

```
[Your machine, residential IP]            [GitHub Actions]
 local_scrape.py  (REDDIT_BACKEND=json)
   scrape .json → extract tickers
   → data/incoming/mentions_<ts>.json
   → git commit + push  ───────────────▶  process_incoming.yml triggers
                                            main.py --from-file <that file>
                                            → yfinance + Claude + score + email
```

Run on your machine (daily, while on a home/residential network):

```powershell
python local_scrape.py --push          # scrape → write file → commit & push (triggers CI)
python local_scrape.py --run-pipeline  # OR do the whole thing locally, no CI, no keys but Anthropic
python local_scrape.py                 # just scrape → write file (no push)
```

Schedule it with Windows Task Scheduler (or cron) once per day. The market-data,
sentiment, scoring, and email steps are **not** IP-blocked, so they run fine in CI
([`process_incoming.yml`](.github/workflows/process_incoming.yml)) — only the
Reddit read has to happen on your IP.

### Official API path (once approved)

Set `REDDIT_BACKEND=praw` + the Reddit secrets, and the scheduled
[`daily_report.yml`](.github/workflows/daily_report.yml) does everything in CI —
no local machine needed. The backend swap is the only change; all downstream code
is identical.

> ⚖️ **ToS note:** unauthenticated `.json` scraping is a gray area even from a
> residential IP. The official Data API (`praw`) is the only fully-sanctioned
> route — the `json` backend is a low-volume personal stopgap while approval is
> pending.

---

## Project layout

```
stock-sentiment-tracker/
├── .github/workflows/
│   ├── daily_report.yml       # API path: scheduled, all-in-CI (REDDIT_BACKEND=praw)
│   └── process_incoming.yml   # no-API path: runs when a scraped file is pushed
├── config.py                  # all constants / weights / secrets / REDDIT_BACKEND
├── main.py                    # pipeline orchestrator (CLI flags incl. --from-file)
├── local_scrape.py            # residential-IP scraper (no-API path entry point)
├── src/
│   ├── database.py            # SQLite schema + all reads/writes (network-free)
│   ├── reddit_scraper.py      # backend dispatch + ticker extraction/validation
│   ├── reddit_json_backend.py # no-API .json scraper (residential IP)
│   ├── stock_fetcher.py       # yfinance market data (retries, caching)
│   ├── sentiment_analyzer.py  # Claude batched classification (prompt-cached)
│   ├── scorer.py              # conviction model
│   ├── report_generator.py    # Jinja2 -> HTML
│   └── email_sender.py        # Gmail SMTP or SendGrid
├── templates/report.html      # dark, mobile-friendly email template
├── tests/test_offline.py      # no-network smoke test
├── data/incoming/             # scraped mention files (no-API path hand-off)
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
