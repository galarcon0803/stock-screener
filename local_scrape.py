"""Local (residential-IP) Reddit scraper — the no-API data path.

Reddit blocks datacenter/CI IPs, so the scrape step runs HERE, on your own
machine, using the public .json backend (no API key). It writes raw,
ticker-validated mentions to data/incoming/mentions_<timestamp>.json and
(optionally) commits+pushes so the CI pipeline picks them up and does the rest
(market data, Claude sentiment, scoring, email).

Usage:
    python local_scrape.py                # scrape -> write file
    python local_scrape.py --push         # also git add/commit/push the file
    python local_scrape.py --run-pipeline # scrape, then run full pipeline locally

Run it once a day (Task Scheduler / cron) while the machine is on a residential
network. Switch to the official API later by setting REDDIT_BACKEND=praw.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

config.configure_logging()
log = logging.getLogger("local_scrape")

import database  # noqa: E402
import reddit_scraper  # noqa: E402

INCOMING_DIR = config.DATA_DIR / "incoming"


def scrape() -> list[dict]:
    """Scrape via the configured backend (json by default) and validate tickers.

    A DB connection is used only for the ticker-validity cache so repeated runs
    don't re-hit yfinance for the same symbols.
    """
    if config.REDDIT_BACKEND != "json":
        log.warning("REDDIT_BACKEND=%s (expected 'json' for local scraping).",
                    config.REDDIT_BACKEND)
    conn = database.init_db(config.DB_PATH)
    try:
        mentions = reddit_scraper.scrape_all_subreddits(conn)
    finally:
        conn.close()
    return mentions


def write_incoming(mentions: list[dict]) -> Path:
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = INCOMING_DIR / f"mentions_{ts}.json"
    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "backend": config.REDDIT_BACKEND,
        "count": len(mentions),
        "mentions": mentions,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %d mentions -> %s", len(mentions), path)
    return path


def git_push(path: Path) -> None:
    rel = path.relative_to(ROOT).as_posix()
    cmds = [
        ["git", "add", rel],
        ["git", "commit", "-m", f"data: scraped mentions {path.stem}"],
        ["git", "push"],
    ]
    for cmd in cmds:
        log.info("$ %s", " ".join(cmd))
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            # commit returns nonzero when there's nothing to commit; tolerate it.
            log.warning("%s -> %s", " ".join(cmd), result.stderr.strip()
                        or result.stdout.strip())
            if cmd[1] == "commit":
                return
    log.info("Pushed %s", rel)


def main() -> int:
    p = argparse.ArgumentParser(description="Local residential-IP Reddit scraper")
    p.add_argument("--push", action="store_true",
                   help="git add/commit/push the incoming file to trigger CI.")
    p.add_argument("--run-pipeline", action="store_true",
                   help="After scraping, run the full pipeline locally from the file.")
    args = p.parse_args()

    try:
        mentions = scrape()
    except Exception as exc:  # noqa: BLE001
        from reddit_json_backend import RedditBlockedError
        if isinstance(exc, RedditBlockedError):
            log.error("Reddit blocked this IP (403). Are you on a datacenter/VPN "
                      "IP? Run from a residential connection, or use the API "
                      "backend. Details: %s", exc)
            return 2
        raise

    if not mentions:
        log.warning("No mentions scraped; nothing written.")
        return 0

    path = write_incoming(mentions)

    if args.push:
        git_push(path)

    if args.run_pipeline:
        import main as pipeline
        log.info("Running full pipeline locally from scraped data...")
        return pipeline.run(pipeline.parse_args(["--from-file", str(path)]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
