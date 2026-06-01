"""Conviction scoring: fuse Reddit signal with market data.

conviction = signal*0.45 + opportunity*0.40 + trend_bonus*0.15
Each sub-score is normalized to 0-100. See config.py for all weights.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

import config
import database

log = logging.getLogger(__name__)

# For aggregation, bearish actively pulls sentiment down (it scores 0.0 as a
# standalone label but should subtract from a ticker's net sentiment).
_SENTIMENT_AGG = dict(config.SENTIMENT_SCORES)
_SENTIMENT_AGG["bearish"] = -0.4


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def score_ticker(ticker: str, mentions: list[dict], stock_data: dict, conn) -> dict:
    """Compute the full score record for one ticker."""
    ticker_mentions = [m for m in mentions if m["ticker"] == ticker]
    mention_count = len(ticker_mentions)

    hist_avg = database.get_historical_mention_avg(conn, ticker, days=7)
    velocity = mention_count / max(hist_avg, 1.0)

    signal = calculate_signal_score(ticker_mentions, hist_avg)
    opportunity = calculate_opportunity_score(stock_data or {})
    trend = calculate_trend_bonus(ticker, conn)

    conviction = (
        signal * config.SCORE_WEIGHTS["signal"]
        + opportunity * config.SCORE_WEIGHTS["opportunity"]
        + trend * config.SCORE_WEIGHTS["trend_bonus"]
    )
    conviction = round(_clamp(conviction), 2)
    tier = config.assign_tier(conviction)

    top_posts = _top_posts(ticker_mentions, n=3)
    top = top_posts[0] if top_posts else {}

    return {
        "ticker": ticker,
        "score_date": date.today().isoformat(),
        "mention_count_24h": mention_count,
        "mention_velocity": round(velocity, 2),
        "signal_score": round(signal, 2),
        "opportunity_score": round(opportunity, 2),
        "trend_bonus": round(trend, 2),
        "conviction_score": conviction,
        "tier": tier,
        "top_post_url": top.get("post_url"),
        "top_post_title": top.get("post_title"),
        # Extras carried for outcome seeding and the report (not all persisted).
        "price": (stock_data or {}).get("price"),
        "stock_data": stock_data or {},
        "top_posts": top_posts,
        "risk_flags": _risk_flags(stock_data or {}),
    }


# --------------------------------------------------------------------------- #
# Signal score
# --------------------------------------------------------------------------- #
def calculate_signal_score(mentions: list[dict], historical_avg: float) -> float:
    if not mentions:
        return 0.0

    count = len(mentions)
    ratio = count / max(historical_avg, 1.0)
    velocity_score = min(100.0, ratio * 25.0)

    sentiment_score = _sentiment_quality(mentions)
    engagement_score = _engagement_depth(mentions)
    avg_weight = sum(
        config.SUBREDDIT_WEIGHTS.get(m["subreddit"], config.DEFAULT_SUBREDDIT_WEIGHT)
        for m in mentions
    ) / count

    signal = (
        velocity_score * config.SIGNAL_WEIGHTS["mention_velocity"]
        + sentiment_score * config.SIGNAL_WEIGHTS["sentiment_quality"]
        + engagement_score * config.SIGNAL_WEIGHTS["engagement_depth"]
        + avg_weight * 100 * config.SIGNAL_WEIGHTS["subreddit_weight"]
    )
    return _clamp(signal)


def _sentiment_quality(mentions: list[dict]) -> float:
    """Engagement-weighted average sentiment, scaled to 0-100.

    Each mention's sentiment is weighted by log(post_score+1) so that
    high-upvote takes dominate. Bearish mentions subtract.
    """
    num = den = 0.0
    for m in mentions:
        label = m.get("sentiment_label", "neutral")
        s = _SENTIMENT_AGG.get(label, config.SENTIMENT_SCORES.get(label, 0.2))
        w = math.log((m.get("post_score") or 0) + 1) + 1.0  # +1 so score-0 posts count
        num += s * w
        den += w
    if den == 0:
        return 0.0
    avg = num / den  # roughly in [-0.4, 1.0]
    return _clamp(avg * 100.0)


def _engagement_depth(mentions: list[dict]) -> float:
    """Blend discussion depth (comments per upvote) with thread recency."""
    now = datetime.now(timezone.utc).timestamp()
    depth_vals, recency_vals = [], []
    for m in mentions:
        score = max(m.get("post_score") or 0, 1)
        comments = m.get("comment_count") or 0
        # Ratio capped: ~0.5 comments/upvote is already very active.
        depth_vals.append(min(1.0, (comments / score) / 0.5))
        age_h = (now - (m.get("post_created_utc") or now)) / 3600
        recency_vals.append(max(0.0, 1.0 - age_h / config.LOOKBACK_HOURS))
    depth = sum(depth_vals) / len(depth_vals)
    recency = sum(recency_vals) / len(recency_vals)
    return _clamp((depth * 0.6 + recency * 0.4) * 100.0)


# --------------------------------------------------------------------------- #
# Opportunity score
# --------------------------------------------------------------------------- #
def calculate_opportunity_score(stock_data: dict) -> float:
    lag = _price_signal_lag(stock_data)
    volume = _volume_expansion(stock_data)
    float_si = _float_short_interest(stock_data)
    penalty = _risk_penalty(stock_data)

    opportunity = (
        lag * config.OPPORTUNITY_WEIGHTS["price_signal_lag"]
        + volume * config.OPPORTUNITY_WEIGHTS["volume_expansion"]
        + float_si * config.OPPORTUNITY_WEIGHTS["float_short_interest"]
        + penalty * config.OPPORTUNITY_WEIGHTS["risk_flags_penalty"]
    )
    return _clamp(opportunity)


def _price_signal_lag(d: dict) -> float:
    """Higher when price hasn't yet moved much (signal ahead of price)."""
    chg_1d = d.get("price_change_1d")
    if chg_1d is None:
        return 50.0  # unknown -> neutral
    if chg_1d < 3:
        score = 90.0
    elif chg_1d <= 8:
        score = 60.0
    else:
        score = 20.0
    # Turning-up bonus: beaten-down over 30d but recovering over 5d.
    chg_5d, chg_30d = d.get("price_change_5d"), d.get("price_change_30d")
    if chg_30d is not None and chg_5d is not None and chg_30d < 0 < chg_5d:
        score += 10
    return _clamp(score)


def _volume_expansion(d: dict) -> float:
    ratio = d.get("volume_ratio")
    if ratio is None:
        return 30.0
    if ratio < 1.0:
        return 20.0
    if ratio < 1.5:
        return 50.0
    if ratio <= 3.0:
        return 80.0
    return 100.0


def _float_short_interest(d: dict) -> float:
    """Squeeze-potential proxy: small float + high short interest is non-linear."""
    float_shares = d.get("float_shares")
    short_pct = d.get("short_interest_pct")
    small_float = float_shares is not None and float_shares < 50_000_000
    high_short = short_pct is not None and short_pct > 15

    if small_float and high_short:
        return 90.0  # the squeeze setup
    score = 40.0  # neutral baseline
    if small_float:
        score += 20
    if high_short:
        score += 20
    return _clamp(score)


def _risk_penalty(d: dict) -> float:
    """Returns 0-100 where 100 = no risk flags (it is added, not subtracted)."""
    score = 100.0
    days = d.get("earnings_days", -1)
    if days is not None and 0 <= days <= 7:
        score -= 50
    elif days is not None and 7 < days <= 14:
        score -= 20
    if (d.get("price_change_5d") or 0) > 15:
        score -= 40
    if not d.get("has_volume_data", False):
        score -= 30
    return _clamp(score)


def _risk_flags(d: dict) -> list[str]:
    flags = []
    days = d.get("earnings_days", -1)
    if days is not None and 0 <= days <= 7:
        flags.append(f"Earnings in {days}d")
    elif days is not None and 7 < days <= 14:
        flags.append(f"Earnings in {days}d")
    if (d.get("price_change_5d") or 0) > 15:
        flags.append(f"Already +{round(d['price_change_5d'],1)}% in 5d")
    if not d.get("has_volume_data", False):
        flags.append("No volume data")
    return flags


# --------------------------------------------------------------------------- #
# Trend bonus (history-aware)
# --------------------------------------------------------------------------- #
def calculate_trend_bonus(ticker: str, conn) -> float:
    """Reward tickers with a good track record; penalize prior duds.

    Returns a 0-100 value (neutral baseline 50) so first-time tickers neither
    gain nor lose from the trend component.
    """
    bonus = 50.0
    history = database.get_ticker_outcome_history(conn, ticker)

    realized = [h for h in history if h.get("return_5d") is not None]
    if realized:
        wins = sum(
            1 for h in realized
            if (h.get("conviction_score_at_signal") or 0) > 60
            and (h.get("return_5d") or 0) > 10
        )
        losses = sum(1 for h in realized if (h.get("return_5d") or 0) < 0)
        bonus += 30 * wins
        bonus -= 20 * losses

    # Persistence: stuck in MONITOR but climbing -> emerging.
    streak = database.get_recent_tier_streak(conn, ticker, "MONITOR", lookback_days=5)
    if streak >= 3:
        bonus += 20

    return _clamp(bonus)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _top_posts(mentions: list[dict], n: int = 3) -> list[dict]:
    posts = [m for m in mentions if m.get("mention_type") == "post"] or mentions
    return sorted(posts, key=lambda m: m.get("post_score") or 0, reverse=True)[:n]


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))
