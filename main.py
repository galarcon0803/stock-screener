"""Pipeline orchestrator for the Stock Sentiment Tracker.

Run order:
  init DB -> backfill past outcomes -> scrape Reddit -> select tickers ->
  fetch market data -> classify sentiment -> persist -> score -> report -> email

Flags:
  --dry-run     run everything except sending email; write report to out/
  --no-email    alias-ish: skip only the send step
  --limit N     cap the number of tickers scored (faster local testing)
  --no-outcomes skip the outcome-backfill step (avoids extra yfinance calls)
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# Make both the repo root (for config.py) and src/ importable.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

config.configure_logging()
log = logging.getLogger("main")

import database  # noqa: E402
import email_sender  # noqa: E402
import report_generator  # noqa: E402
import reddit_scraper  # noqa: E402
import scorer  # noqa: E402
import stock_fetcher  # noqa: E402


def select_tickers(mentions: list[dict], limit: int | None = None) -> list[str]:
    """Tickers meeting the minimum-mention threshold, most-mentioned first."""
    counts = Counter(m["ticker"] for m in mentions)
    ranked = [t for t, c in counts.most_common() if c >= config.MIN_MENTIONS_TO_SCORE]
    if limit:
        ranked = ranked[:limit]
    log.info("%d/%d tickers meet the >=%d mention threshold%s",
             len(ranked), len(counts), config.MIN_MENTIONS_TO_SCORE,
             f" (limited to {limit})" if limit else "")
    return ranked


def run(args: argparse.Namespace) -> int:
    today = date.today().isoformat()
    if getattr(args, "preset", None):
        _apply_preset(args.preset)
    conn = database.init_db(config.DB_PATH)

    try:
        # 1. Backfill 5d/30d returns for prior signals.
        if not args.no_outcomes:
            database.update_outcomes(conn)

        # 2. Obtain mentions: either from a pre-scraped file (the no-API path,
        #    where local_scrape.py did the residential-IP scrape) or by scraping
        #    directly here (works with REDDIT_BACKEND=praw, or json on a
        #    residential IP).
        if args.from_file:
            mentions = load_mentions_file(args.from_file)
        else:
            mentions = reddit_scraper.scrape_all_subreddits(conn)
        if not mentions:
            log.warning("No mentions available; nothing to do.")
            return 0

        # 3. Select tickers above threshold.
        tickers = select_tickers(mentions, limit=args.limit)
        if not tickers:
            log.warning("No tickers crossed the mention threshold.")

        # Keep only mentions for selected tickers (cheaper sentiment + scoring).
        selected = set(tickers)
        mentions = [m for m in mentions if m["ticker"] in selected]

        # 4. Market data.
        stock_data = stock_fetcher.fetch_stock_data(tickers)

        # 5. Sentiment classification (mutates mentions in place).
        scorer_input = mentions
        if not args.no_sentiment:
            scorer_input = sentiment_step(mentions)
        else:
            log.info("Skipping sentiment step (--no-sentiment)")

        # 6. Persist raw data.
        database.insert_mentions(conn, scorer_input)
        for ticker in tickers:
            if ticker in stock_data:
                database.insert_stock_snapshot(conn, ticker, stock_data[ticker])

        # 7. Score.
        scores = []
        for ticker in tickers:
            score = scorer.score_ticker(
                ticker, scorer_input, stock_data.get(ticker, {}), conn
            )
            scores.append(score)
            database.insert_ticker_score(conn, score)
        _log_tiers(scores)

        # 8. Report.
        html = report_generator.generate_report(scores, run_date=today)
        out_path = ROOT / "out" / f"report_{today}.html"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        log.info("Report written to %s", out_path)

        # 9. Email.
        if args.dry_run or args.no_email:
            log.info("Email skipped (%s).",
                     "dry-run" if args.dry_run else "--no-email")
        else:
            counts = Counter(s["tier"] for s in scores)
            subject = (
                f"[Stock Tracker] Daily Report — {today} | "
                f"{counts.get('ACT', 0)} ACT, {counts.get('WATCH', 0)} WATCH"
            )
            email_sender.send_report(html, subject)

        return 0
    finally:
        conn.close()


def load_mentions_file(path: str) -> list[dict]:
    """Load pre-scraped mentions written by local_scrape.py."""
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    mentions = data.get("mentions", data) if isinstance(data, dict) else data
    log.info("Loaded %d mentions from %s (scraped_at=%s)",
             len(mentions), path,
             data.get("scraped_at") if isinstance(data, dict) else "?")
    return mentions


def sentiment_step(mentions: list[dict]) -> list[dict]:
    import sentiment_analyzer

    return sentiment_analyzer.analyze_batch(mentions)


def _log_tiers(scores: list[dict]) -> None:
    counts = Counter(s["tier"] for s in scores)
    log.info("Scored %d tickers — ACT:%d WATCH:%d MONITOR:%d NOISE:%d",
             len(scores), counts.get("ACT", 0), counts.get("WATCH", 0),
             counts.get("MONITOR", 0), counts.get("NOISE", 0))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stock Sentiment Tracker pipeline")
    p.add_argument("--dry-run", action="store_true",
                   help="Run everything but do not send email.")
    p.add_argument("--no-email", action="store_true", help="Skip the email send.")
    p.add_argument("--no-outcomes", action="store_true",
                   help="Skip outcome backfill (saves yfinance calls).")
    p.add_argument("--no-sentiment", action="store_true",
                   help="Skip Claude sentiment classification (for offline tests).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap number of tickers scored.")
    p.add_argument("--from-file", type=str, default=None,
                   help="Load mentions from a local_scrape.py JSON file instead "
                        "of scraping (the no-API CI path).")
    p.add_argument("--preset", choices=["quick", "full"], default=None,
                   help="quick = 4 biggest subs/2 sorts (fast); full = all subs.")
    return p.parse_args(argv)


# Quick preset: the highest-signal subs + fewer sorts, for fast/cheap runs.
_QUICK_SUBS = ["wallstreetbets", "stocks", "smallstreetbets", "options"]


def _apply_preset(name: str) -> None:
    if name == "quick":
        config.SUBREDDITS = _QUICK_SUBS
        config.DEFAULT_SORTS = ("hot", "new")
        config.POSTS_PER_SUBREDDIT = 40
        log.info("Preset 'quick': %d subs, sorts=%s", len(config.SUBREDDITS),
                 config.DEFAULT_SORTS)
    elif name == "full":
        log.info("Preset 'full': all %d subs", len(config.SUBREDDITS))


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
