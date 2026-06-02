"""Central configuration: constants, weights, and environment access.

Everything tunable lives here so the scoring model and scrape behavior can be
adjusted without touching module logic. Secrets are read from environment
variables (loaded from a local .env in dev, or GitHub Secrets in CI).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# Load .env if present (no-op when the file is absent, e.g. in CI).
try:
    from dotenv import load_dotenv

    # override=True so values in .env win over pre-existing (possibly empty)
    # shell vars — e.g. an empty ANTHROPIC_API_KEY exported in the environment.
    load_dotenv(override=True)
except ImportError:  # python-dotenv not installed; rely on real env vars.
    pass


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
TEMPLATES_DIR = ROOT_DIR / "templates"
DB_PATH = str(DATA_DIR / "sentiment.db")


# --------------------------------------------------------------------------- #
# Environment / secrets
# --------------------------------------------------------------------------- #
def env(key: str, default: str | None = None, required: bool = False) -> str | None:
    """Fetch an environment variable, optionally enforcing presence."""
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"Required environment variable '{key}' is not set.")
    return value


# Reddit
REDDIT_CLIENT_ID = env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = env("REDDIT_USER_AGENT", "StockSentimentTracker/1.0")

# Which scraping backend to use:
#   "browser" — drives a real Chrome via Playwright. Beats Reddit's anti-bot
#               wall that now 403s plain HTTP clients. No API key. Local only.
#               THIS IS THE WORKING NO-API DEFAULT.
#   "json"    — raw .json via urllib. Lighter, but Reddit currently 403s it
#               (bot detection), so it usually fails. Kept for when/if that eases.
#   "praw"    — official OAuth Data API (needs the credentials above). Works from
#               CI once the Data API request is approved.
REDDIT_BACKEND = (env("REDDIT_BACKEND", "browser") or "browser").lower()
# Run the browser headless? Reddit detects headless more easily, so default False.
BROWSER_HEADLESS = (env("BROWSER_HEADLESS", "false") or "false").lower() == "true"
# Which browser Playwright drives. Default "msedge" (Edge) so scraping uses a
# DIFFERENT browser than your everyday Chrome — closing the scraper's instance
# never touches your Chrome windows. Options: "msedge" | "chrome" | "chromium".
BROWSER_CHANNEL = env("BROWSER_CHANNEL", "msedge")
# Browser-like UA for the json backend (Reddit 403s obvious bot UAs less, but the
# real gate is IP type). Overridable via env.
REDDIT_JSON_USER_AGENT = env(
    "REDDIT_JSON_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
REDDIT_JSON_SLEEP_SECONDS = 3.0   # base delay between Reddit requests (anti-429)

# Rate-limit handling for the browser/json backends. Reddit 429s aggressively
# when comment fetches fire back-to-back, so we throttle and back off.
REDDIT_REQUEST_DELAY = 3.0        # seconds between every Reddit request
REDDIT_429_BACKOFF = 30.0         # seconds to wait after a 429 before retrying
REDDIT_429_MAX_RETRIES = 3        # retries per request on 429 before giving up
REDDIT_MAX_COMMENT_FETCHES = 15   # cap comment-page fetches PER SUBREDDIT (the
                                  # main 429 driver); top posts are fetched first
REDDIT_PAUSE_BETWEEN_SUBS = 5.0   # extra pause between subreddits

# Anthropic
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")

# Email
EMAIL_PROVIDER = (env("EMAIL_PROVIDER", "gmail") or "gmail").lower()
GMAIL_ADDRESS = env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD")
SENDGRID_API_KEY = env("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = env("SENDGRID_FROM_EMAIL")
REPORT_EMAIL_TO = env("REPORT_EMAIL_TO")


# --------------------------------------------------------------------------- #
# Claude / sentiment
# --------------------------------------------------------------------------- #
# Sentiment classification is a lightweight task. Sonnet gives strong DD-vs-FOMO
# discrimination at reasonable cost; switch to "claude-haiku-4-5-20251001" to cut
# cost further if quality is acceptable.
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-sonnet-4-6")
SENTIMENT_BATCH_SIZE = 20          # mentions per Claude API call
SENTIMENT_MAX_TOKENS = 4096

# USD per million tokens, for run-cost reporting. Keyed by a substring of the
# model name. Update if Anthropic pricing changes. (cache_write/read approximated
# from standard 1.25x / 0.1x multipliers on input.)
CLAUDE_PRICING = {
    "sonnet": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "haiku":  {"input": 0.80, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08},
    "opus":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
}


def model_pricing(model: str = CLAUDE_MODEL) -> dict:
    """Return the per-million-token pricing dict for the configured model."""
    for key, prices in CLAUDE_PRICING.items():
        if key in model.lower():
            return prices
    return CLAUDE_PRICING["sonnet"]  # safe default

# Sentiment label -> base score used in signal aggregation.
SENTIMENT_SCORES = {
    "bullish_dd": 1.0,
    "bullish_general": 0.7,
    "bullish_fomo": 0.4,
    "neutral": 0.2,
    "question": 0.1,
    "bearish": 0.0,
}
VALID_SENTIMENT_LABELS = set(SENTIMENT_SCORES.keys())


# --------------------------------------------------------------------------- #
# Reddit scraping
# --------------------------------------------------------------------------- #
SUBREDDITS = [
    "wallstreetbets",
    "TheRaceTo10Million",
    "stocks",
    "stockstobuytoday",
    "options",
    "optionsmillionaire",
    "undervaluedstonks",
    "wallstreetbetsnew",
    "stockmarket",
    "smallstreetbets",
    "biotech_stocks",
    "optionstrading",
    "daytrading",
    "thetagang",
]

SUBREDDIT_WEIGHTS = {
    "wallstreetbets": 1.0,
    "TheRaceTo10Million": 1.3,
    "undervaluedstonks": 1.4,
    "smallstreetbets": 1.2,
    "biotech_stocks": 1.3,
    "thetagang": 1.2,
    "optionsmillionaire": 1.1,
    "optionstrading": 1.0,
    "stocks": 1.0,
    "stockmarket": 0.9,
    "stockstobuytoday": 0.8,
    "wallstreetbetsnew": 0.8,
    "daytrading": 0.9,
    "options": 1.0,
}
DEFAULT_SUBREDDIT_WEIGHT = 1.0

POSTS_PER_SUBREDDIT = 100          # cap per sort per subreddit per run
COMMENT_DEPTH = 2                  # comment levels parsed per post
COMMENTS_PER_POST = 40             # cap comments inspected per post
LOOKBACK_HOURS = 24               # ignore posts older than this
CONTENT_SNIPPET_LEN = 500

# Sorts pulled per subreddit. WSB additionally gets "controversial" to surface
# buried/downvoted high-signal posts (handled in the scraper).
DEFAULT_SORTS = ("hot", "new", "top")
WSB_EXTRA_SORTS = ("controversial",)


# --------------------------------------------------------------------------- #
# Ticker extraction
# --------------------------------------------------------------------------- #
# Uppercase tokens that look like tickers but never are, in this context.
TICKER_BLACKLIST = {
    "I", "A", "THE", "FOR", "ARE", "AND", "OR", "BUT", "NOT", "YOU", "ALL",
    "ANY", "CAN", "HAD", "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET",
    "HAS", "HIM", "HIS", "HOW", "MAN", "NEW", "NOW", "OLD", "SEE", "TWO",
    "WAY", "WHO", "BOY", "DID", "ITS", "LET", "PUT", "SAY", "SHE", "TOO",
    "USE", "WSB", "YOLO", "DD", "OG", "CEO", "CFO", "FDA", "SEC", "IPO",
    "ETF", "ATH", "ATL", "EOD", "IMO", "TBH", "FWIW", "EPS", "PE", "OP",
    "USA", "USD", "GDP", "CPI", "FED", "FOMO", "LOL", "LMAO", "WTF", "TLDR",
    "EV", "AI", "ER", "PR", "EU", "UK", "US", "ITM", "OTM", "FD", "CALL",
    "PUT", "PUTS", "CALLS", "BUY", "SELL", "HOLD", "MOON", "BTW", "GG",
    "RIP", "YTD", "QOQ", "YOY", "ROI", "PSA", "ELI", "AMA", "TA", "RH",
    "IRA", "401K", "HODL", "WSJ", "CNBC", "NYSE", "OTC", "GMI", "NGMI",
    # Added after the 2026-06-01 live run surfaced these as false positives.
    # Common acronyms / words that happen to be valid yfinance symbols:
    "NASA",   # people typing NASA, not the leveraged ETF
    "DRAM",   # memory tech term, not the ticker
    "FCF",    # "free cash flow"
    "UP",     # the word "up"
    "ELON", "EOY", "EOM", "DCA", "ATM", "GTC", "AH", "PM", "RE",
    "IV", "OI", "PT", "FY", "YE", "WL",
    "IIRC", "AFAIK", "IMHO", "FUD", "BS", "OK", "NO", "YES", "IDK",
    "GUH", "FOMC", "DJIA",
    # Added after the full 14-sub run surfaced these English words as tickers.
    # NOTE: deliberately NOT blacklisting OPEN (Opendoor) — a real meme stock.
    "BACK", "GO", "PC", "LINE", "PUMP", "BC", "JUST", "NEED", "SETUP",
    "FACTS", "BTC", "ETH", "GROW", "PLAY", "PEAK", "REAL", "STEP",
    "TITLE", "LOVE", "HOPE", "CASH", "RIDE", "WELL", "FAST", "HUGE",
    "BIG", "LOW", "HIGH", "NICE", "GAIN", "LOSS", "RISK", "CARE", "HELP",
    "TIME", "WORK", "LIFE", "FREE", "SAFE", "BEST", "GOOD", "BOLD", "FORM",
    # Broad-market index/benchmark ETFs — valid tickers but noise for a
    # single-name discussion tracker (the whole sub talks about "SPY"/"the market"):
    "SPY", "VOO", "VTI", "IWM", "DIA",
}

# $TICKER form (highest confidence) accepts 1-5 chars (e.g. $F, $T).
# Bare uppercase tokens require 2-5 chars: single letters (I, A, S, P, X, U, T)
# are almost always English/noise, not tickers, so we don't trust them un-cashtagged.
TICKER_CASHTAG_RE = r"\$([A-Za-z]{1,5})\b"
TICKER_BARE_RE = r"\b([A-Z]{2,5})\b"


# --------------------------------------------------------------------------- #
# Scoring model
# --------------------------------------------------------------------------- #
MIN_MENTIONS_TO_SCORE = 3          # tickers below this are ignored

CONVICTION_TIERS = {
    "ACT": (80, 100),
    "WATCH": (60, 79),
    "MONITOR": (40, 59),
    "NOISE": (0, 39),
}

SCORE_WEIGHTS = {
    "signal": 0.45,
    "opportunity": 0.40,
    "trend_bonus": 0.15,
}

SIGNAL_WEIGHTS = {
    "mention_velocity": 0.30,
    "sentiment_quality": 0.35,
    "engagement_depth": 0.20,
    "subreddit_weight": 0.15,
}

OPPORTUNITY_WEIGHTS = {
    "price_signal_lag": 0.35,
    "volume_expansion": 0.25,
    "float_short_interest": 0.25,
    "risk_flags_penalty": 0.15,
}

# Earnings within this many days flips an `earnings_near` risk flag.
EARNINGS_NEAR_DAYS = 14


# --------------------------------------------------------------------------- #
# yfinance fetch behavior
# --------------------------------------------------------------------------- #
YF_SLEEP_SECONDS = 0.5             # politeness delay between ticker fetches
YF_MAX_RETRIES = 3


def assign_tier(score: float) -> str:
    """Map a conviction score (0-100) to a tier label."""
    for tier, (lo, hi) in CONVICTION_TIERS.items():
        if lo <= score <= hi:
            return tier
    return "NOISE"


def configure_logging(level: int = logging.INFO) -> None:
    """Set up consistent, timestamped logging across all modules."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
