"""SQLite persistence layer.

Owns the schema and every read/write. Network-free by design: the one function
that needs live prices (`update_outcomes`) accepts a price-fetch callable so this
module stays importable and testable without yfinance.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS reddit_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    post_id TEXT NOT NULL,
    post_title TEXT,
    post_url TEXT,
    post_score INTEGER,
    post_upvote_ratio REAL,
    comment_count INTEGER,
    post_created_utc INTEGER,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    mention_type TEXT,
    sentiment_label TEXT,
    sentiment_score REAL,
    sentiment_summary TEXT,
    content_snippet TEXT,
    UNIQUE(ticker, post_id, mention_type)
);

CREATE TABLE IF NOT EXISTS stock_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    price REAL,
    price_change_1d REAL,
    price_change_5d REAL,
    price_change_30d REAL,
    volume BIGINT,
    avg_volume_30d BIGINT,
    volume_ratio REAL,
    market_cap REAL,
    float_shares REAL,
    short_interest_pct REAL,
    week_52_high REAL,
    week_52_low REAL,
    distance_from_52w_high REAL,
    earnings_date TEXT,
    UNIQUE(ticker, snapshot_date)
);

CREATE TABLE IF NOT EXISTS ticker_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    score_date DATE NOT NULL,
    mention_count_24h INTEGER,
    mention_velocity REAL,
    signal_score REAL,
    opportunity_score REAL,
    trend_bonus REAL,
    conviction_score REAL,
    tier TEXT,
    top_post_url TEXT,
    top_post_title TEXT,
    UNIQUE(ticker, score_date)
);

CREATE TABLE IF NOT EXISTS ticker_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    signal_date DATE NOT NULL,
    conviction_score_at_signal REAL,
    price_at_signal REAL,
    price_5d_after REAL,
    price_30d_after REAL,
    return_5d REAL,
    return_30d REAL,
    outcome_recorded_at TIMESTAMP,
    UNIQUE(ticker, signal_date)
);

-- One-time ticker validity decisions so we never re-check the same symbol.
CREATE TABLE IF NOT EXISTS ticker_validation_cache (
    ticker TEXT PRIMARY KEY,
    is_valid INTEGER NOT NULL,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mentions_ticker ON reddit_mentions(ticker);
CREATE INDEX IF NOT EXISTS idx_mentions_captured ON reddit_mentions(captured_at);
CREATE INDEX IF NOT EXISTS idx_scores_date ON ticker_scores(score_date);
CREATE INDEX IF NOT EXISTS idx_outcomes_ticker ON ticker_outcomes(ticker);
"""


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #
def init_db(db_path: str) -> sqlite3.Connection:
    """Open (creating dirs as needed) the DB, apply schema, return connection."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("Database ready at %s", db_path)
    return conn


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def insert_mentions(conn: sqlite3.Connection, mentions: Iterable[dict]) -> int:
    """Upsert mention rows. Dedup via UNIQUE(ticker, post_id, mention_type)."""
    rows = [
        (
            m["ticker"], m["subreddit"], m["post_id"], m.get("post_title"),
            m.get("post_url"), m.get("post_score"), m.get("post_upvote_ratio"),
            m.get("comment_count"), m.get("post_created_utc"),
            m.get("mention_type"), m.get("sentiment_label"),
            m.get("sentiment_score"), m.get("sentiment_summary"),
            m.get("content_snippet"),
        )
        for m in mentions
    ]
    cur = conn.executemany(
        """
        INSERT INTO reddit_mentions (
            ticker, subreddit, post_id, post_title, post_url, post_score,
            post_upvote_ratio, comment_count, post_created_utc, mention_type,
            sentiment_label, sentiment_score, sentiment_summary, content_snippet
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, post_id, mention_type) DO UPDATE SET
            post_score=excluded.post_score,
            post_upvote_ratio=excluded.post_upvote_ratio,
            comment_count=excluded.comment_count,
            sentiment_label=COALESCE(excluded.sentiment_label, sentiment_label),
            sentiment_score=COALESCE(excluded.sentiment_score, sentiment_score),
            sentiment_summary=COALESCE(excluded.sentiment_summary, sentiment_summary)
        """,
        rows,
    )
    conn.commit()
    log.info("Stored %d mention rows", len(rows))
    return cur.rowcount


def insert_stock_snapshot(conn: sqlite3.Connection, ticker: str, data: dict) -> None:
    """Upsert today's market snapshot for a ticker."""
    snapshot_date = data.get("snapshot_date") or date.today().isoformat()
    conn.execute(
        """
        INSERT INTO stock_snapshots (
            ticker, snapshot_date, price, price_change_1d, price_change_5d,
            price_change_30d, volume, avg_volume_30d, volume_ratio, market_cap,
            float_shares, short_interest_pct, week_52_high, week_52_low,
            distance_from_52w_high, earnings_date
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, snapshot_date) DO UPDATE SET
            price=excluded.price,
            price_change_1d=excluded.price_change_1d,
            price_change_5d=excluded.price_change_5d,
            price_change_30d=excluded.price_change_30d,
            volume=excluded.volume,
            avg_volume_30d=excluded.avg_volume_30d,
            volume_ratio=excluded.volume_ratio,
            market_cap=excluded.market_cap,
            float_shares=excluded.float_shares,
            short_interest_pct=excluded.short_interest_pct,
            week_52_high=excluded.week_52_high,
            week_52_low=excluded.week_52_low,
            distance_from_52w_high=excluded.distance_from_52w_high,
            earnings_date=excluded.earnings_date
        """,
        (
            ticker, snapshot_date, data.get("price"), data.get("price_change_1d"),
            data.get("price_change_5d"), data.get("price_change_30d"),
            data.get("volume"), data.get("avg_volume_30d"),
            data.get("volume_ratio"), data.get("market_cap"),
            data.get("float_shares"), data.get("short_interest_pct"),
            data.get("week_52_high"), data.get("week_52_low"),
            data.get("distance_from_52w_high"), data.get("earnings_date"),
        ),
    )
    conn.commit()


def insert_ticker_score(conn: sqlite3.Connection, score: dict) -> None:
    """Upsert a ticker's daily conviction score, and seed an outcome row for
    actionable tiers so returns can be backfilled later."""
    score_date = score.get("score_date") or date.today().isoformat()
    conn.execute(
        """
        INSERT INTO ticker_scores (
            ticker, score_date, mention_count_24h, mention_velocity,
            signal_score, opportunity_score, trend_bonus, conviction_score,
            tier, top_post_url, top_post_title
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, score_date) DO UPDATE SET
            mention_count_24h=excluded.mention_count_24h,
            mention_velocity=excluded.mention_velocity,
            signal_score=excluded.signal_score,
            opportunity_score=excluded.opportunity_score,
            trend_bonus=excluded.trend_bonus,
            conviction_score=excluded.conviction_score,
            tier=excluded.tier,
            top_post_url=excluded.top_post_url,
            top_post_title=excluded.top_post_title
        """,
        (
            score["ticker"], score_date, score.get("mention_count_24h"),
            score.get("mention_velocity"), score.get("signal_score"),
            score.get("opportunity_score"), score.get("trend_bonus"),
            score.get("conviction_score"), score.get("tier"),
            score.get("top_post_url"), score.get("top_post_title"),
        ),
    )
    if score.get("tier") in ("ACT", "WATCH") and score.get("price") is not None:
        _seed_outcome(conn, score["ticker"], score_date,
                      score.get("conviction_score"), score.get("price"))
    conn.commit()


def _seed_outcome(conn, ticker, signal_date, conviction, price) -> None:
    conn.execute(
        """
        INSERT INTO ticker_outcomes (
            ticker, signal_date, conviction_score_at_signal, price_at_signal,
            outcome_recorded_at
        ) VALUES (?,?,?,?,?)
        ON CONFLICT(ticker, signal_date) DO NOTHING
        """,
        (ticker, signal_date, conviction, price, datetime.utcnow().isoformat()),
    )


def cache_ticker_validity(conn, ticker: str, is_valid: bool) -> None:
    conn.execute(
        """INSERT INTO ticker_validation_cache (ticker, is_valid)
           VALUES (?, ?)
           ON CONFLICT(ticker) DO UPDATE SET is_valid=excluded.is_valid,
                                             checked_at=CURRENT_TIMESTAMP""",
        (ticker, 1 if is_valid else 0),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_cached_ticker_validity(conn, ticker: str) -> bool | None:
    """Return cached validity, or None if the ticker has never been checked."""
    row = conn.execute(
        "SELECT is_valid FROM ticker_validation_cache WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return None if row is None else bool(row["is_valid"])


def get_seen_post_ids(conn, days: int = 7) -> set[str]:
    """Post IDs already captured recently, so re-runs skip them (saves requests
    and avoids double-counting). Bounded to a trailing window so the set stays
    small and very old posts could in principle be re-seen."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT post_id FROM reddit_mentions WHERE date(captured_at) >= ?",
        (since,),
    ).fetchall()
    return {r["post_id"] for r in rows}


def get_historical_mention_avg(conn, ticker: str, days: int = 7) -> float:
    """Average daily mention count over the trailing `days` (excluding today)."""
    since = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM reddit_mentions
        WHERE ticker = ?
          AND date(captured_at) >= ?
          AND date(captured_at) < ?
        """,
        (ticker, since, today),
    ).fetchone()
    total = row["total"] if row else 0
    return total / days if days else 0.0


def get_ticker_outcome_history(conn, ticker: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM ticker_outcomes
        WHERE ticker = ?
        ORDER BY signal_date DESC
        """,
        (ticker,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_tier_streak(conn, ticker: str, tier: str, lookback_days: int = 5) -> int:
    """Count consecutive most-recent days (up to lookback) the ticker held `tier`."""
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        """
        SELECT score_date, tier FROM ticker_scores
        WHERE ticker = ? AND score_date >= ? AND score_date < ?
        ORDER BY score_date DESC
        """,
        (ticker, since, date.today().isoformat()),
    ).fetchall()
    streak = 0
    for r in rows:
        if r["tier"] == tier:
            streak += 1
        else:
            break
    return streak


def get_tickers_for_report(conn, report_date: str) -> list[dict]:
    """Return all scored tickers for a date, richest first, joined with the
    day's market snapshot."""
    rows = conn.execute(
        """
        SELECT s.*, snap.price, snap.price_change_1d, snap.price_change_5d,
               snap.price_change_30d, snap.volume_ratio, snap.market_cap,
               snap.float_shares, snap.short_interest_pct,
               snap.distance_from_52w_high, snap.earnings_date
        FROM ticker_scores s
        LEFT JOIN stock_snapshots snap
               ON snap.ticker = s.ticker AND snap.snapshot_date = s.score_date
        WHERE s.score_date = ?
        ORDER BY s.conviction_score DESC
        """,
        (report_date,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Outcome backfill
# --------------------------------------------------------------------------- #
def update_outcomes(
    conn,
    price_fetcher: Callable[[str], float | None] | None = None,
) -> int:
    """Backfill 5d/30d returns for past signals once enough time has elapsed.

    `price_fetcher(ticker) -> price|None` is injected so this module stays
    network-free. If omitted, lazily imports stock_fetcher.get_current_price.
    Returns the number of outcome rows updated.
    """
    if price_fetcher is None:
        from stock_fetcher import get_current_price as price_fetcher  # lazy

    today = date.today()
    pending = conn.execute(
        """
        SELECT id, ticker, signal_date, price_at_signal, return_5d, return_30d
        FROM ticker_outcomes
        WHERE price_at_signal IS NOT NULL
          AND (return_5d IS NULL OR return_30d IS NULL)
        """
    ).fetchall()

    updated = 0
    price_cache: dict[str, float | None] = {}
    for row in pending:
        sig_date = date.fromisoformat(str(row["signal_date"]))
        age = (today - sig_date).days
        need_5d = row["return_5d"] is None and age >= 5
        need_30d = row["return_30d"] is None and age >= 30
        if not (need_5d or need_30d):
            continue

        ticker = row["ticker"]
        if ticker not in price_cache:
            try:
                price_cache[ticker] = price_fetcher(ticker)
            except Exception as exc:  # noqa: BLE001 - resilience over correctness
                log.warning("Outcome price fetch failed for %s: %s", ticker, exc)
                price_cache[ticker] = None
        current = price_cache[ticker]
        if current is None:
            continue

        base = row["price_at_signal"]
        sets, params = [], []
        if need_5d:
            sets += ["price_5d_after = ?", "return_5d = ?"]
            params += [current, _pct(base, current)]
        if need_30d:
            sets += ["price_30d_after = ?", "return_30d = ?"]
            params += [current, _pct(base, current)]
        sets.append("outcome_recorded_at = ?")
        params.append(datetime.utcnow().isoformat())
        params.append(row["id"])
        conn.execute(
            f"UPDATE ticker_outcomes SET {', '.join(sets)} WHERE id = ?", params
        )
        updated += 1

    conn.commit()
    if updated:
        log.info("Backfilled outcomes for %d signals", updated)
    return updated


def _pct(base: float, current: float) -> float | None:
    if not base:
        return None
    return round((current - base) / base * 100, 2)


# --------------------------------------------------------------------------- #
# Analytics / dashboard queries (history-aware)
# --------------------------------------------------------------------------- #
def get_all_score_dates(conn) -> list[str]:
    """Every date we have scores for, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT score_date FROM ticker_scores ORDER BY score_date DESC"
    ).fetchall()
    return [r["score_date"] for r in rows]


def get_latest_scores_with_trend(conn, day: str | None = None) -> list[dict]:
    """Latest-day scores joined with that day's snapshot, PLUS each ticker's
    prior-day conviction and how many days it has appeared. This is what makes
    the dashboard useful: momentum (rising/cooling) and persistence, not just a
    static snapshot."""
    if day is None:
        dates = get_all_score_dates(conn)
        if not dates:
            return []
        day = dates[0]

    rows = conn.execute(
        """
        SELECT s.*, snap.price, snap.price_change_1d, snap.price_change_5d,
               snap.price_change_30d, snap.volume_ratio, snap.market_cap,
               snap.short_interest_pct, snap.distance_from_52w_high
        FROM ticker_scores s
        LEFT JOIN stock_snapshots snap
               ON snap.ticker = s.ticker AND snap.snapshot_date = s.score_date
        WHERE s.score_date = ?
        ORDER BY s.conviction_score DESC
        """,
        (day,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Prior appearance (any earlier date) for momentum + days-tracked.
        prior = conn.execute(
            """SELECT conviction_score, score_date FROM ticker_scores
               WHERE ticker = ? AND score_date < ?
               ORDER BY score_date DESC LIMIT 1""",
            (d["ticker"], day),
        ).fetchone()
        appearances = conn.execute(
            "SELECT COUNT(*) n FROM ticker_scores WHERE ticker = ?",
            (d["ticker"],),
        ).fetchone()["n"]
        d["prev_conviction"] = prior["conviction_score"] if prior else None
        d["conviction_delta"] = (
            round(d["conviction_score"] - prior["conviction_score"], 1)
            if prior else None
        )
        d["is_new"] = prior is None
        d["days_tracked"] = appearances
        # Sentiment mix for this ticker (lifetime).
        d["sentiment_mix"] = _sentiment_mix_for(conn, d["ticker"])
        out.append(d)
    return out


def _sentiment_mix_for(conn, ticker: str) -> dict:
    rows = conn.execute(
        """SELECT sentiment_label, COUNT(*) n FROM reddit_mentions
           WHERE ticker = ? AND sentiment_label IS NOT NULL
           GROUP BY sentiment_label""",
        (ticker,),
    ).fetchall()
    return {r["sentiment_label"]: r["n"] for r in rows}


def get_ticker_history(conn, ticker: str) -> dict:
    """Full per-ticker history for a detail view: score timeline, price
    snapshots, recent mentions, and realized outcomes."""
    scores = [dict(r) for r in conn.execute(
        """SELECT score_date, conviction_score, signal_score, opportunity_score,
                  trend_bonus, tier, mention_count_24h
           FROM ticker_scores WHERE ticker = ? ORDER BY score_date""",
        (ticker,),
    ).fetchall()]
    snaps = [dict(r) for r in conn.execute(
        """SELECT snapshot_date, price, price_change_1d, volume_ratio
           FROM stock_snapshots WHERE ticker = ? ORDER BY snapshot_date""",
        (ticker,),
    ).fetchall()]
    mentions = [dict(r) for r in conn.execute(
        """SELECT subreddit, post_title, post_url, post_score, sentiment_label,
                  sentiment_summary, captured_at
           FROM reddit_mentions WHERE ticker = ?
           ORDER BY post_score DESC LIMIT 15""",
        (ticker,),
    ).fetchall()]
    outcomes = get_ticker_outcome_history(conn, ticker)
    return {"ticker": ticker, "scores": scores, "snapshots": snaps,
            "top_mentions": mentions, "outcomes": outcomes}


def get_model_performance(conn) -> dict:
    """Did the model's signals actually pay off? Aggregates realized outcomes
    (backfilled 5d/30d returns) so the dashboard can show a real track record."""
    rows = conn.execute(
        """SELECT conviction_score_at_signal AS conv, return_5d, return_30d
           FROM ticker_outcomes WHERE return_5d IS NOT NULL"""
    ).fetchall()
    realized = [dict(r) for r in rows]
    n = len(realized)
    perf = {"n_signals": n, "n_pending": 0, "avg_return_5d": None,
            "avg_return_30d": None, "win_rate_5d": None, "by_tier": {}}
    pend = conn.execute(
        "SELECT COUNT(*) n FROM ticker_outcomes WHERE return_5d IS NULL"
    ).fetchone()
    perf["n_pending"] = pend["n"] if pend else 0
    if n:
        r5 = [r["return_5d"] for r in realized if r["return_5d"] is not None]
        r30 = [r["return_30d"] for r in realized if r["return_30d"] is not None]
        perf["avg_return_5d"] = round(sum(r5) / len(r5), 2) if r5 else None
        perf["avg_return_30d"] = round(sum(r30) / len(r30), 2) if r30 else None
        wins = sum(1 for v in r5 if v > 0)
        perf["win_rate_5d"] = round(wins / len(r5) * 100, 1) if r5 else None
    return perf


def get_trending_tickers(conn, limit: int = 8) -> list[dict]:
    """Biggest conviction risers vs their previous appearance (momentum)."""
    dates = get_all_score_dates(conn)
    if len(dates) < 2:
        return []
    today, prev = dates[0], dates[1]
    rows = conn.execute(
        """SELECT t.ticker, t.conviction_score AS now_c, p.conviction_score AS prev_c
           FROM ticker_scores t
           JOIN ticker_scores p ON p.ticker = t.ticker AND p.score_date = ?
           WHERE t.score_date = ?""",
        (prev, today),
    ).fetchall()
    movers = [{"ticker": r["ticker"],
               "delta": round(r["now_c"] - r["prev_c"], 1),
               "now": r["now_c"]} for r in rows]
    movers.sort(key=lambda m: m["delta"], reverse=True)
    return movers[:limit]


def get_db_stats(conn) -> dict:
    """High-level coverage stats for the dashboard header."""
    def one(q):
        return conn.execute(q).fetchone()[0]
    return {
        "total_mentions": one("SELECT COUNT(*) FROM reddit_mentions"),
        "total_scores": one("SELECT COUNT(*) FROM ticker_scores"),
        "distinct_tickers": one("SELECT COUNT(DISTINCT ticker) FROM ticker_scores"),
        "days_of_data": one("SELECT COUNT(DISTINCT score_date) FROM ticker_scores"),
        "first_date": one("SELECT MIN(score_date) FROM ticker_scores"),
        "last_date": one("SELECT MAX(score_date) FROM ticker_scores"),
    }
