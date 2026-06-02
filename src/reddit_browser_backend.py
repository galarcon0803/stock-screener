"""No-API Reddit backend driving a real Chrome via Playwright.

Reddit now bot-blocks plain HTTP clients (urllib/requests/headless) with a 403
"network security" page regardless of IP or User-Agent. A real, non-headless
Chrome passes the check: we load reddit.com once to clear the bot challenge, then
fetch the same `.json` endpoints in that browser context.

Requires (one-time):
    pip install playwright
    playwright install chromium      # or use channel="chrome" with installed Chrome

Returns the same dict shape as the other backends (see reddit_scraper._mention)
so extraction/validation upstream is backend-agnostic.

ToS note: same gray-area caveat as the .json backend. The sanctioned path is the
OAuth Data API (REDDIT_BACKEND=praw) once approved.
"""

from __future__ import annotations

import json
import logging
import time

import config

log = logging.getLogger(__name__)


class RedditBlockedError(RuntimeError):
    """Raised when even a real browser is blocked (challenge not cleared)."""


# --------------------------------------------------------------------------- #
# Browser session (one warm browser reused for the whole run)
# --------------------------------------------------------------------------- #
def _channel_fallback(preferred: str) -> list[str]:
    """Ordered channels to try: preferred first, then the rest, chromium last."""
    order = [preferred] + [c for c in ("msedge", "chrome", "chromium")
                           if c != preferred]
    return order


class _Session:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Launch the configured channel (default Edge) so we don't share a
        # process with the user's everyday Chrome. Fall back to other channels,
        # then bundled chromium. Playwright closes ONLY this instance on exit —
        # never taskkill a browser by image name.
        channels = _channel_fallback(config.BROWSER_CHANNEL)
        last_err = None
        for ch in channels:
            try:
                if ch == "chromium":
                    self._browser = self._pw.chromium.launch(
                        headless=config.BROWSER_HEADLESS)
                else:
                    self._browser = self._pw.chromium.launch(
                        channel=ch, headless=config.BROWSER_HEADLESS)
                log.info("Launched browser channel=%s", ch)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning("Browser channel '%s' unavailable: %s", ch,
                            str(exc)[:60])
        if self._browser is None:
            raise RuntimeError(f"No usable browser channel: {last_err}")

        ctx = self._browser.new_context(user_agent=config.REDDIT_JSON_USER_AGENT)
        self._page = ctx.new_page()
        self._warm_up()
        return self

    def __exit__(self, *exc):
        # Clean, scoped shutdown of ONLY the instance we launched. Never kill
        # browsers by image name — that would close the user's other windows.
        try:
            if self._browser:
                self._browser.close()
        except Exception as e:  # noqa: BLE001
            log.warning("browser close failed: %s", str(e)[:60])
        finally:
            try:
                if self._pw:
                    self._pw.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("playwright stop failed: %s", str(e)[:60])

    def _warm_up(self, raise_on_fail: bool = True) -> bool:
        """Visit reddit.com to clear the anti-bot challenge. Returns True on
        success. The cleared cookie expires after a few minutes, so this is also
        called mid-run to re-clear when a fetch starts getting blocked."""
        try:
            self._page.goto("https://www.reddit.com/", timeout=45000)
            self._page.wait_for_timeout(2500)
            title = (self._page.title() or "").lower()
            ok = "blocked" not in title and "network security" not in title
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.debug("warm-up navigation error: %s", str(exc)[:60])
        if ok:
            log.info("Browser warm-up OK (%s)", self._page.title()[:40])
            return True
        if raise_on_fail:
            raise RedditBlockedError("Bot challenge not cleared on warm-up.")
        log.warning("Re-warm failed (still challenged).")
        return False

    def get_json(self, url: str):
        """Fetch a Reddit .json URL. Handles 429 (backoff) and 403/block (the
        anti-bot cookie expired mid-run -> re-warm and retry). Returns parsed
        JSON, or None if it can't be fetched after retries."""
        rewarmed = False
        for attempt in range(config.REDDIT_429_MAX_RETRIES + 1):
            status, body, err = self._navigate(url)
            blocked = status == 403 or (err and "403" in err)

            if blocked:
                # The warm-up cookie expires after a few minutes; re-clear the
                # challenge once and retry rather than aborting the whole run.
                if not rewarmed:
                    log.warning("403 on r/%s — cookie expired, re-warming session",
                                url.split('/r/')[-1].split('/')[0])
                    time.sleep(config.REDDIT_REQUEST_DELAY)
                    if self._warm_up(raise_on_fail=False):
                        rewarmed = True
                        continue
                raise RedditBlockedError(f"403 for {url} after re-warm attempt.")

            if status == 429:
                if attempt < config.REDDIT_429_MAX_RETRIES:
                    wait = config.REDDIT_429_BACKOFF * (attempt + 1)
                    log.warning("429 on %s — backing off %.0fs (retry %d/%d)",
                                url.split("/r/")[-1][:40], wait, attempt + 1,
                                config.REDDIT_429_MAX_RETRIES)
                    time.sleep(wait)
                    continue
                log.warning("429 persisted on %s — giving up", url)
                return None

            if status != 200:
                # Promoted from debug to warning so silent failures are visible.
                log.warning("fetch failed (%s) for r/%s: %s", status or "err",
                            url.split('/r/')[-1].split('/')[0],
                            (err or '')[:60])
                return None

            time.sleep(config.REDDIT_REQUEST_DELAY)
            try:
                return json.loads(body)
            except (json.JSONDecodeError, TypeError):
                log.warning("Non-JSON body for %s: %s", url, (body or "")[:80])
                return None
        return None

    def _navigate(self, url: str):
        """Returns (status, body, error_str). Playwright raises on some non-2xx
        (ERR_HTTP_RESPONSE_CODE_FAILURE) instead of returning a response."""
        try:
            resp = self._page.goto(url, timeout=30000)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            # Extract a status code if present in the error text.
            for code in ("403", "429", "404", "500", "503"):
                if code in msg:
                    return int(code), None, msg
            return 0, None, msg
        status = resp.status if resp else 0
        body = self._page.inner_text("body") if status == 200 else None
        return status, body, None


# --------------------------------------------------------------------------- #
# Listing / comment parsing (shared shape with reddit_json_backend)
# --------------------------------------------------------------------------- #
def _sort_path(sort: str) -> str:
    if sort in ("top", "controversial"):
        return f"{sort}.json?t=day&limit={config.POSTS_PER_SUBREDDIT}&raw_json=1"
    return f"{sort}.json?limit={config.POSTS_PER_SUBREDDIT}&raw_json=1"


def _walk_comments(children, out, depth):
    if depth >= config.COMMENT_DEPTH:
        return
    for child in children:
        if child.get("kind") != "t1":
            continue
        cd = child.get("data", {})
        if cd.get("body"):
            out.append(cd["body"])
        replies = cd.get("replies")
        if isinstance(replies, dict):
            _walk_comments(replies.get("data", {}).get("children", []), out, depth + 1)


def _post_record(subreddit_name, d):
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


# --------------------------------------------------------------------------- #
# Public: scrape all subreddits in one browser session
# --------------------------------------------------------------------------- #
def scrape_all_browser(subreddits, sorts_for, extract_fn, is_recent_fn,
                       snippet_len, seen_post_ids=None):
    """Scrape every subreddit via one warm Chrome session. Injected callables
    keep extraction logic in reddit_scraper. Throttled + 429-aware; pauses
    between subreddits. Posts whose id is in `seen_post_ids` are skipped (already
    captured in a prior run)."""
    seen_post_ids = seen_post_ids or set()
    out: list[dict] = []
    with _Session() as sess:
        for i, name in enumerate(subreddits):
            if i > 0:
                time.sleep(config.REDDIT_PAUSE_BETWEEN_SUBS)
            out.extend(_scrape_one(sess, name, sorts_for(name), extract_fn,
                                   is_recent_fn, snippet_len, seen_post_ids))
    return out


def _scrape_one(sess, name, sorts, extract_fn, is_recent_fn, snippet_len,
                seen_post_ids):
    out: list[dict] = []
    seen: set = set()
    skipped_seen = 0

    # Pass 1: collect listings across sorts; harvest post-level mentions from
    # ALL fresh posts, and remember which are worth a (rate-limited) comment fetch.
    candidates: dict[str, dict] = {}  # post_id -> post record
    for sort in sorts:
        url = f"https://www.reddit.com/r/{name}/{_sort_path(sort)}"
        data = sess.get_json(url)
        if not isinstance(data, dict):
            continue
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            if not d or not is_recent_fn(d.get("created_utc", 0)):
                continue
            pid = d.get("id", "")
            if pid in seen_post_ids:        # already captured in a prior run
                skipped_seen += 1
                continue
            post = _post_record(name, d)
            post_text = f"{d.get('title','')}\n{d.get('selftext','') or ''}"
            for ticker in extract_fn(post_text):
                key = (pid, ticker, "post")
                if key in seen:
                    continue
                seen.add(key)
                out.append({**post, "ticker": ticker, "mention_type": "post",
                            "content_snippet": post_text[:snippet_len]})
            if (d.get("num_comments") or 0) > 0:
                candidates[pid] = post  # dedup across sorts by post id

    # Pass 2: comments only for the top-N highest-scoring NEW posts, capped to
    # limit requests (comment fetches are the main 429 driver).
    top = sorted(candidates.values(), key=lambda p: p["post_score"], reverse=True)
    for post in top[: config.REDDIT_MAX_COMMENT_FETCHES]:
        pid = post["post_id"]
        curl = (f"https://www.reddit.com/r/{name}/comments/{pid}.json"
                f"?limit={config.COMMENTS_PER_POST}&depth={config.COMMENT_DEPTH}"
                f"&raw_json=1")
        cdata = sess.get_json(curl)
        bodies: list[str] = []
        if isinstance(cdata, list) and len(cdata) > 1:
            _walk_comments(cdata[1].get("data", {}).get("children", []), bodies, 0)
        for body in bodies[: config.COMMENTS_PER_POST]:
            for ticker in extract_fn(body):
                key = (pid, ticker, "comment")
                if key in seen:
                    continue
                seen.add(key)
                out.append({**post, "ticker": ticker, "mention_type": "comment",
                            "content_snippet": body[:snippet_len]})

    log.info("r/%s (browser) -> %d mentions | %d new posts, comments on top %d, "
             "%d seen-skipped", name, len(out), len(candidates),
             min(len(candidates), config.REDDIT_MAX_COMMENT_FETCHES), skipped_seen)
    return out
