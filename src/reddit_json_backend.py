"""No-API Reddit backend using the public `.json` endpoints.

Reddit exposes the same data the website uses at `…/r/<sub>/<sort>.json` and
`…/comments/<id>.json` with no API key. The catch: Reddit 403-blocks datacenter
IPs (CI runners, cloud hosts), so this backend is intended to run from a
residential IP (your own machine, via local_scrape.py). On a blocked IP every
request raises HTTP 403 — that's expected, not a bug.

This module deliberately returns the same dict shape that the PRAW path produces
(see reddit_scraper._mention), so the extraction/validation logic above it is
backend-agnostic.

NOTE on Reddit's Terms: unauthenticated `.json` scraping is a gray area even from
a residential IP. The sanctioned path is the OAuth Data API (REDDIT_BACKEND=praw)
once approved. This backend exists as a stopgap and for low-volume personal use.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config

log = logging.getLogger(__name__)


class RedditBlockedError(RuntimeError):
    """Raised when Reddit returns 403 — almost always a datacenter-IP block."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _fetch_json(url: str) -> dict | list:
    """GET a Reddit .json URL with a browser-like UA and polite delay."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": config.REDDIT_JSON_USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise RedditBlockedError(
                f"Reddit returned {exc.code} for {url}. If running from a "
                f"datacenter/CI IP this is expected — use a residential IP or "
                f"switch REDDIT_BACKEND=praw."
            ) from exc
        raise
    finally:
        time.sleep(config.REDDIT_JSON_SLEEP_SECONDS)
    return data


# --------------------------------------------------------------------------- #
# Listing / comment parsing
# --------------------------------------------------------------------------- #
def _sort_path(sort: str) -> str:
    # top/controversial need ?t=day; hot/new are plain.
    if sort in ("top", "controversial"):
        return f"{sort}.json?t=day&limit={config.POSTS_PER_SUBREDDIT}&raw_json=1"
    return f"{sort}.json?limit={config.POSTS_PER_SUBREDDIT}&raw_json=1"


def _listing_children(subreddit: str, sort: str) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/{_sort_path(sort)}"
    data = _fetch_json(url)
    if isinstance(data, dict):
        return data.get("data", {}).get("children", [])
    return []


def _fetch_comment_bodies(subreddit: str, post_id: str) -> list[str]:
    """Return up to COMMENTS_PER_POST comment bodies within COMMENT_DEPTH levels."""
    url = (
        f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json"
        f"?limit={config.COMMENTS_PER_POST}&depth={config.COMMENT_DEPTH}&raw_json=1"
    )
    try:
        data = _fetch_json(url)
    except RedditBlockedError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.debug("comment fetch failed for %s: %s", post_id, exc)
        return []
    bodies: list[str] = []
    # data[1] is the comment listing; data[0] is the post itself.
    if isinstance(data, list) and len(data) > 1:
        _walk_comments(data[1].get("data", {}).get("children", []), bodies, 0)
    return bodies[: config.COMMENTS_PER_POST]


def _walk_comments(children: list[dict], out: list[str], depth: int) -> None:
    if depth >= config.COMMENT_DEPTH:
        return
    for child in children:
        if child.get("kind") != "t1":
            continue
        cd = child.get("data", {})
        body = cd.get("body")
        if body:
            out.append(body)
        replies = cd.get("replies")
        if isinstance(replies, dict):
            _walk_comments(
                replies.get("data", {}).get("children", []), out, depth + 1
            )


# --------------------------------------------------------------------------- #
# Public: scrape one subreddit (returns mention dicts via injected extractor)
# --------------------------------------------------------------------------- #
def scrape_subreddit_json(
    subreddit_name: str,
    sorts: tuple[str, ...],
    extract_fn,
    is_recent_fn,
    snippet_len: int,
) -> list[dict]:
    """Scrape a subreddit through .json. `extract_fn(text)->list[str]` and
    `is_recent_fn(created_utc)->bool` are injected from reddit_scraper to keep
    extraction logic in one place."""
    out: list[dict] = []
    seen: set[tuple] = set()

    for sort in sorts:
        try:
            children = _listing_children(subreddit_name, sort)
        except RedditBlockedError:
            raise  # propagate: a 403 means the whole backend is unusable here
        except Exception as exc:  # noqa: BLE001
            log.warning("r/%s %s .json failed: %s", subreddit_name, sort, exc)
            continue

        for child in children:
            d = child.get("data", {})
            if not d or not is_recent_fn(d.get("created_utc", 0)):
                continue

            post = _post_record(subreddit_name, d, snippet_len)
            post_text = f"{d.get('title','')}\n{d.get('selftext','') or ''}"
            for ticker in extract_fn(post_text):
                key = (post["post_id"], ticker, "post")
                if key in seen:
                    continue
                seen.add(key)
                out.append({**post, "ticker": ticker, "mention_type": "post",
                            "content_snippet": post_text[:snippet_len]})

            # Comments (best-effort; skip if the post has none worth fetching).
            if (d.get("num_comments") or 0) > 0:
                try:
                    bodies = _fetch_comment_bodies(subreddit_name, d.get("id"))
                except RedditBlockedError:
                    raise
                for body in bodies:
                    for ticker in extract_fn(body):
                        key = (post["post_id"], ticker, "comment")
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({**post, "ticker": ticker,
                                    "mention_type": "comment",
                                    "content_snippet": body[:snippet_len]})

    log.info("r/%s (json) -> %d raw mentions", subreddit_name, len(out))
    return out


def _post_record(subreddit_name: str, d: dict, snippet_len: int) -> dict:
    permalink = d.get("permalink", "")
    return {
        "subreddit": subreddit_name,
        "post_id": d.get("id", ""),
        "post_title": d.get("title", ""),
        "post_url": f"https://reddit.com{permalink}" if permalink else "",
        "post_score": int(d.get("score", 0) or 0),
        "post_upvote_ratio": float(d.get("upvote_ratio", 0.0) or 0.0),
        "comment_count": int(d.get("num_comments", 0) or 0),
        "post_created_utc": int(d.get("created_utc", 0) or 0),
    }
