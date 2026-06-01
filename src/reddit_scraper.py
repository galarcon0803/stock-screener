"""Reddit scraping and ticker extraction.

Pulls posts (and shallow comment trees) across the configured subreddits and
multiple sort orders, extracts ticker mentions, validates them (cache-first,
yfinance fallback), and returns one structured dict per (ticker, source).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import config

log = logging.getLogger(__name__)

_CASHTAG_RE = re.compile(config.TICKER_CASHTAG_RE)
_BARE_RE = re.compile(config.TICKER_BARE_RE)


# --------------------------------------------------------------------------- #
# Reddit client
# --------------------------------------------------------------------------- #
def _get_reddit():
    """Construct a read-only PRAW client. check_for_async=False avoids the
    event-loop warning in non-async runners (GitHub Actions)."""
    import praw

    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        raise RuntimeError("Reddit credentials are not configured.")
    return praw.Reddit(
        client_id=config.REDDIT_CLIENT_ID,
        client_secret=config.REDDIT_CLIENT_SECRET,
        user_agent=config.REDDIT_USER_AGENT,
        check_for_async=False,
    )


# --------------------------------------------------------------------------- #
# Ticker extraction
# --------------------------------------------------------------------------- #
def extract_tickers_from_text(text: str) -> list[str]:
    """Return candidate tickers found in `text`, deduped, order-preserving.

    $CASHTAG form is trusted broadly; bare uppercase tokens are only accepted
    when not in the blacklist (final validity is confirmed by is_valid_ticker).
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    for m in _CASHTAG_RE.finditer(text):
        t = m.group(1).upper()
        if t not in seen and t not in config.TICKER_BLACKLIST:
            seen.add(t)
            found.append(t)

    for m in _BARE_RE.finditer(text):
        t = m.group(1)
        if t not in seen and t not in config.TICKER_BLACKLIST:
            seen.add(t)
            found.append(t)

    return found


def is_valid_ticker(ticker: str, conn=None) -> bool:
    """Validate a symbol against yfinance, using the DB cache when available.

    A symbol is valid if yfinance returns a recent price. Results are cached
    permanently (symbols don't stop existing often, and the cost of a rare
    miss is far lower than re-validating thousands of tokens per run).
    """
    if conn is not None:
        cached = None
        try:
            import database

            cached = database.get_cached_ticker_validity(conn, ticker)
        except Exception:  # noqa: BLE001
            cached = None
        if cached is not None:
            return cached

    valid = _yf_symbol_exists(ticker)

    if conn is not None:
        try:
            import database

            database.cache_ticker_validity(conn, ticker, valid)
        except Exception:  # noqa: BLE001
            pass
    return valid


def _fast_price(yt) -> float | None:
    """Robustly read last price from yfinance fast_info (attr, not .get())."""
    try:
        fi = yt.fast_info
        price = getattr(fi, "last_price", None)
        if price is None and hasattr(fi, "get"):
            price = fi.get("lastPrice")
        return float(price) if price else None
    except Exception:  # noqa: BLE001
        return None


def _yf_symbol_exists(ticker: str) -> bool:
    try:
        import yfinance as yf

        time.sleep(config.YF_SLEEP_SECONDS)  # politeness on cache-miss path
        # NB: fast_info.get('last_price') returns None in current yfinance;
        # the attribute access is the reliable path.
        price = _fast_price(yf.Ticker(ticker))
        return price is not None and price > 0
    except Exception:  # noqa: BLE001 - any failure means "treat as invalid"
        return False


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def _is_recent(created_utc: float) -> bool:
    age_h = (datetime.now(timezone.utc).timestamp() - created_utc) / 3600
    return age_h <= config.LOOKBACK_HOURS


def _mention(ticker, sub, submission, mention_type, text) -> dict:
    snippet = (text or "")[: config.CONTENT_SNIPPET_LEN]
    return {
        "ticker": ticker,
        "subreddit": sub,
        "post_id": submission.id,
        "post_title": submission.title,
        "post_url": f"https://reddit.com{submission.permalink}",
        "post_score": int(getattr(submission, "score", 0) or 0),
        "post_upvote_ratio": float(getattr(submission, "upvote_ratio", 0.0) or 0.0),
        "comment_count": int(getattr(submission, "num_comments", 0) or 0),
        "post_created_utc": int(submission.created_utc),
        "mention_type": mention_type,
        "content_snippet": snippet,
    }


def _sorts_for(subreddit_name: str) -> tuple[str, ...]:
    sorts = list(config.DEFAULT_SORTS)
    if subreddit_name.lower() == "wallstreetbets":
        sorts += list(config.WSB_EXTRA_SORTS)
    return tuple(sorts)


def _iter_submissions(subreddit, sort: str):
    limit = config.POSTS_PER_SUBREDDIT
    if sort == "hot":
        return subreddit.hot(limit=limit)
    if sort == "new":
        return subreddit.new(limit=limit)
    if sort == "top":
        return subreddit.top(time_filter="day", limit=limit)
    if sort == "controversial":
        return subreddit.controversial(time_filter="day", limit=limit)
    return subreddit.hot(limit=limit)


def scrape_subreddit(reddit, subreddit_name: str, weight: float) -> list[dict]:
    """Scrape one subreddit across its sorts; return raw mention dicts.

    Tickers are extracted but NOT yet validated (validation is batched by the
    caller to dedupe network calls). Dedupes by (post_id, ticker, mention_type)
    within this subreddit pass.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    subreddit = reddit.subreddit(subreddit_name)

    for sort in _sorts_for(subreddit_name):
        try:
            submissions = _iter_submissions(subreddit, sort)
        except Exception as exc:  # noqa: BLE001
            log.warning("r/%s %s listing failed: %s", subreddit_name, sort, exc)
            continue

        for sub in submissions:
            try:
                if not _is_recent(sub.created_utc):
                    continue

                # Title + selftext as the "post" mention source.
                post_text = f"{sub.title}\n{getattr(sub, 'selftext', '') or ''}"
                for ticker in extract_tickers_from_text(post_text):
                    key = (sub.id, ticker, "post")
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(_mention(ticker, subreddit_name, sub, "post", post_text))

                # Shallow comment scan.
                _scrape_comments(sub, subreddit_name, seen, out)
            except Exception as exc:  # noqa: BLE001 - never let one post kill the run
                log.debug("Skipping a submission in r/%s: %s", subreddit_name, exc)
                continue

    log.info("r/%s -> %d raw mentions", subreddit_name, len(out))
    return out


def _scrape_comments(submission, subreddit_name, seen, out) -> None:
    try:
        submission.comments.replace_more(limit=0)
        comments = submission.comments.list()[: config.COMMENTS_PER_POST]
    except Exception:  # noqa: BLE001
        return
    for c in comments:
        depth = getattr(c, "depth", 0)
        if depth is not None and depth >= config.COMMENT_DEPTH:
            continue
        body = getattr(c, "body", "") or ""
        for ticker in extract_tickers_from_text(body):
            key = (submission.id, ticker, "comment")
            if key in seen:
                continue
            seen.add(key)
            m = _mention(ticker, subreddit_name, submission, "comment", body)
            out.append(m)


def _gather_raw_praw() -> list[dict]:
    reddit = _get_reddit()
    raw: list[dict] = []
    for name in config.SUBREDDITS:
        weight = config.SUBREDDIT_WEIGHTS.get(name, config.DEFAULT_SUBREDDIT_WEIGHT)
        raw.extend(scrape_subreddit(reddit, name, weight))
    return raw


def _gather_raw_json() -> list[dict]:
    """Gather raw mentions via the public .json backend (residential IP)."""
    import reddit_json_backend as backend

    raw: list[dict] = []
    for name in config.SUBREDDITS:
        raw.extend(
            backend.scrape_subreddit_json(
                name,
                _sorts_for(name),
                extract_tickers_from_text,
                _is_recent,
                config.CONTENT_SNIPPET_LEN,
            )
        )
    return raw


def _gather_raw_browser(seen_post_ids: set[str] | None = None) -> list[dict]:
    """Gather raw mentions by driving a real Chrome (beats the anti-bot wall)."""
    import reddit_browser_backend as backend

    return backend.scrape_all_browser(
        config.SUBREDDITS,
        _sorts_for,
        extract_tickers_from_text,
        _is_recent,
        config.CONTENT_SNIPPET_LEN,
        seen_post_ids=seen_post_ids or set(),
    )


def gather_raw_mentions(conn=None) -> list[dict]:
    """Backend-dispatching raw scrape (no ticker validation yet).

    When a DB connection is given, posts already captured in the recent window
    are skipped so re-runs don't re-pull or double-count them (and we issue
    fewer requests, easing rate limits)."""
    seen_post_ids: set[str] = set()
    if conn is not None:
        try:
            import database

            seen_post_ids = database.get_seen_post_ids(conn)
            if seen_post_ids:
                log.info("Skipping %d already-seen posts from recent runs",
                         len(seen_post_ids))
        except Exception:  # noqa: BLE001
            seen_post_ids = set()

    if config.REDDIT_BACKEND == "praw":
        return _gather_raw_praw()
    if config.REDDIT_BACKEND == "json":
        return _gather_raw_json()
    if config.REDDIT_BACKEND == "browser":
        return _gather_raw_browser(seen_post_ids)
    raise RuntimeError(f"Unknown REDDIT_BACKEND: {config.REDDIT_BACKEND!r}")


def validate_and_filter(raw: list[dict], conn=None) -> list[dict]:
    """Validate each unique ticker once, drop mentions for invalid symbols."""
    log.info("Validating %d candidate mentions", len(raw))
    validity: dict[str, bool] = {}
    kept: list[dict] = []
    for m in raw:
        t = m["ticker"]
        if t not in validity:
            validity[t] = is_valid_ticker(t, conn)
        if validity[t]:
            kept.append(m)
    valid_count = sum(1 for v in validity.values() if v)
    log.info("Validation: %d/%d unique symbols valid, %d mentions kept",
             valid_count, len(validity), len(kept))
    return kept


def scrape_all_subreddits(conn=None) -> list[dict]:
    """Scrape every configured subreddit (per REDDIT_BACKEND), validate tickers
    once each, and return the filtered list of mention dicts."""
    raw = gather_raw_mentions(conn)
    return validate_and_filter(raw, conn)
