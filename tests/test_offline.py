"""Offline smoke test: exercises extraction, DB, scoring, and report rendering
with mock data only (no Reddit / yfinance / Claude network calls)."""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config
import database
import reddit_scraper
import scorer
import report_generator


def test_ticker_extraction():
    text = "I'm all in on $NVDA and AMD, but THE market hates TSLA. YOLO into GME!"
    found = reddit_scraper.extract_tickers_from_text(text)
    assert "NVDA" in found and "AMD" in found and "TSLA" in found and "GME" in found
    assert "THE" not in found and "YOLO" not in found and "I" not in found
    print("  extraction:", found)


def _mock_mentions():
    now = int(time.time())
    rows = []
    for i in range(6):
        rows.append({
            "ticker": "NVDA", "subreddit": "wallstreetbets",
            "post_id": f"p{i}", "post_title": f"NVDA breakout play {i}",
            "post_url": f"https://reddit.com/r/wsb/p{i}", "post_score": 100 + i * 50,
            "post_upvote_ratio": 0.95, "comment_count": 40 + i,
            "post_created_utc": now - i * 1800, "mention_type": "post",
            "sentiment_label": "bullish_dd" if i % 2 == 0 else "bullish_general",
            "sentiment_score": 1.0 if i % 2 == 0 else 0.7,
            "sentiment_summary": "Strong datacenter demand thesis",
            "content_snippet": "Detailed DD on datacenter revenue growth...",
        })
    for i in range(4):
        rows.append({
            "ticker": "GME", "subreddit": "smallstreetbets",
            "post_id": f"g{i}", "post_title": f"GME squeeze incoming {i}",
            "post_url": f"https://reddit.com/r/ssb/g{i}", "post_score": 20 + i * 10,
            "post_upvote_ratio": 0.6, "comment_count": 5 + i,
            "post_created_utc": now - i * 3600, "mention_type": "post",
            "sentiment_label": "bullish_fomo", "sentiment_score": 0.4,
            "sentiment_summary": "Moon hype, no substance",
            "content_snippet": "TO THE MOON rockets diamond hands...",
        })
    return rows


def _mock_stock(ticker):
    base = {
        "NVDA": {"price": 130.5, "price_change_1d": 1.8, "price_change_5d": 4.2,
                 "price_change_30d": -6.0, "volume_ratio": 2.4,
                 "market_cap": 3.2e12, "float_shares": 2.4e9,
                 "short_interest_pct": 1.2, "earnings_days": 25,
                 "has_volume_data": True},
        "GME": {"price": 24.1, "price_change_1d": 11.0, "price_change_5d": 18.0,
                "price_change_30d": 30.0, "volume_ratio": 5.0,
                "market_cap": 9e9, "float_shares": 40e6,
                "short_interest_pct": 22.0, "earnings_days": 4,
                "has_volume_data": True},
    }[ticker]
    base.update({"ticker": ticker, "snapshot_date": None})
    return base


def main():
    test_ticker_extraction()

    db_path = str(ROOT / "data" / "test_offline.db")
    Path(db_path).unlink(missing_ok=True)
    conn = database.init_db(db_path)

    mentions = _mock_mentions()
    database.insert_mentions(conn, mentions)

    scores = []
    for ticker in ["NVDA", "GME"]:
        sd = _mock_stock(ticker)
        sd["snapshot_date"] = None
        database.insert_stock_snapshot(conn, ticker, sd)
        score = scorer.score_ticker(ticker, mentions, sd, conn)
        database.insert_ticker_score(conn, score)
        scores.append(score)
        print(f"  {ticker}: conviction={score['conviction_score']} "
              f"tier={score['tier']} signal={score['signal_score']} "
              f"opp={score['opportunity_score']} trend={score['trend_bonus']}")

    html = report_generator.generate_report(scores)
    out = ROOT / "out" / "report_test.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    assert "Stock Sentiment Tracker" in html
    assert "NVDA" in html and "GME" in html
    print(f"  report rendered -> {out} ({len(html)} bytes)")

    # Outcome seeding check.
    outcomes = database.get_ticker_outcome_history(conn, "NVDA")
    print(f"  NVDA outcomes seeded: {len(outcomes)}")
    conn.close()
    print("OK: offline smoke test passed")


if __name__ == "__main__":
    main()
