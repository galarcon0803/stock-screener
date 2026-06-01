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

    load_dotenv()
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
#   "json" — Reddit's public .json endpoints, no API key. Works only from a
#            residential IP (datacenter IPs like CI runners get 403'd), so this
#            is what local_scrape.py uses on your own machine.
#   "praw" — official OAuth Data API (needs the credentials above). Use once the
#            Data API request is approved; works from CI.
REDDIT_BACKEND = (env("REDDIT_BACKEND", "json") or "json").lower()
# Browser-like UA for the json backend (Reddit 403s obvious bot UAs less, but the
# real gate is IP type). Overridable via env.
REDDIT_JSON_USER_AGENT = env(
    "REDDIT_JSON_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
REDDIT_JSON_SLEEP_SECONDS = 1.5   # politeness delay between .json requests

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
}

# $TICKER form (highest confidence) and bare uppercase 1-5 letter tokens.
TICKER_CASHTAG_RE = r"\$([A-Za-z]{1,5})\b"
TICKER_BARE_RE = r"\b([A-Z]{1,5})\b"


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
