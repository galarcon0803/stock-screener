"""Market data retrieval via yfinance.

yfinance is flaky for small/illiquid tickers and gets rate-limited, so every
fetch is wrapped in retries + try/except and results are cached per run.
Missing fields degrade to None rather than failing the pipeline.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import config

log = logging.getLogger(__name__)

_RUN_CACHE: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def fetch_stock_data(tickers: list[str]) -> dict[str, dict]:
    """Fetch market data for each ticker. Returns {ticker: data_dict}.

    Tickers that fail entirely are omitted from the result so callers can
    detect missing data and penalize accordingly.
    """
    import yfinance as yf

    out: dict[str, dict] = {}
    for ticker in tickers:
        if ticker in _RUN_CACHE:
            out[ticker] = _RUN_CACHE[ticker]
            continue
        data = _fetch_one(yf, ticker)
        if data is not None:
            _RUN_CACHE[ticker] = data
            out[ticker] = data
        time.sleep(config.YF_SLEEP_SECONDS)
    log.info("Fetched market data for %d/%d tickers", len(out), len(tickers))
    return out


def get_current_price(ticker: str) -> float | None:
    """Lightweight current-price lookup (used for outcome backfill)."""
    import yfinance as yf

    for attempt in range(config.YF_MAX_RETRIES):
        try:
            fast = yf.Ticker(ticker).fast_info
            price = fast.get("last_price") if hasattr(fast, "get") else None
            if price:
                return float(price)
        except Exception as exc:  # noqa: BLE001
            log.debug("price retry %d for %s: %s", attempt + 1, ticker, exc)
            time.sleep(config.YF_SLEEP_SECONDS * (attempt + 1))
    return None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _fetch_one(yf, ticker: str) -> dict | None:
    for attempt in range(config.YF_MAX_RETRIES):
        try:
            return _build_record(yf, ticker)
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch retry %d for %s: %s", attempt + 1, ticker, exc)
            time.sleep(config.YF_SLEEP_SECONDS * (attempt + 1))
    log.warning("Giving up on market data for %s", ticker)
    return None


def _build_record(yf, ticker: str) -> dict:
    yt = yf.Ticker(ticker)
    # 1y of daily history powers price-change and average-volume math.
    hist = yt.history(period="1y", auto_adjust=False)
    info = _safe_info(yt)

    record: dict = {
        "ticker": ticker,
        "snapshot_date": date.today().isoformat(),
        "price": None,
        "price_change_1d": None,
        "price_change_5d": None,
        "price_change_30d": None,
        "volume": None,
        "avg_volume_30d": None,
        "volume_ratio": None,
        "market_cap": info.get("marketCap"),
        "float_shares": info.get("floatShares") or info.get("sharesOutstanding"),
        "short_interest_pct": _short_interest_pct(info),
        "week_52_high": info.get("fiftyTwoWeekHigh"),
        "week_52_low": info.get("fiftyTwoWeekLow"),
        "distance_from_52w_high": None,
        "earnings_date": None,
        "earnings_days": -1,
        "earnings_near": False,
        "has_volume_data": False,
    }

    if hist is not None and not hist.empty:
        record.update(calculate_price_changes(ticker, hist))

    # 52-week distance: prefer info's high, fall back to history max.
    high = record["week_52_high"]
    if not high and hist is not None and not hist.empty:
        high = float(hist["Close"].max())
        record["week_52_high"] = high
    if record["price"] and high:
        record["distance_from_52w_high"] = round(
            (high - record["price"]) / high * 100, 2
        )

    days = get_earnings_proximity(ticker, yt)
    record["earnings_days"] = days
    if 0 <= days <= config.EARNINGS_NEAR_DAYS:
        record["earnings_near"] = True
    record["earnings_date"] = _earnings_date_str(yt)

    return record


def calculate_price_changes(ticker: str, hist) -> dict:
    """Compute price, 1/5/30-day % changes and volume metrics from history."""
    closes = hist["Close"].dropna()
    volumes = hist["Volume"].dropna()
    out: dict = {}
    if closes.empty:
        return out

    price = float(closes.iloc[-1])
    out["price"] = round(price, 4)
    out["price_change_1d"] = _pct_change(closes, 1)
    out["price_change_5d"] = _pct_change(closes, 5)
    out["price_change_30d"] = _pct_change(closes, 21)  # ~21 trading days/month

    if not volumes.empty:
        today_vol = int(volumes.iloc[-1])
        avg_30 = int(volumes.tail(30).mean()) if len(volumes) >= 1 else None
        out["volume"] = today_vol
        out["avg_volume_30d"] = avg_30
        out["volume_ratio"] = (
            round(today_vol / avg_30, 2) if avg_30 else None
        )
        out["has_volume_data"] = True
    return out


def get_earnings_proximity(ticker: str, yt=None) -> int:
    """Days until next earnings; -1 if unknown."""
    try:
        import yfinance as yf

        yt = yt or yf.Ticker(ticker)
        cal = yt.calendar
        ed = None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, (list, tuple)) and ed:
                ed = ed[0]
        if ed is None:
            return -1
        ed_dt = _coerce_date(ed)
        if ed_dt is None:
            return -1
        return (ed_dt - date.today()).days
    except Exception:  # noqa: BLE001
        return -1


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _pct_change(series, periods: int) -> float | None:
    if len(series) <= periods:
        return None
    past = float(series.iloc[-1 - periods])
    if not past:
        return None
    return round((float(series.iloc[-1]) - past) / past * 100, 2)


def _safe_info(yt) -> dict:
    try:
        info = yt.info
        return info if isinstance(info, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _short_interest_pct(info: dict) -> float | None:
    val = info.get("shortPercentOfFloat")
    if val is None:
        return None
    # yfinance reports this as a fraction (0.15 == 15%).
    return round(val * 100, 2) if val < 1.5 else round(val, 2)


def _earnings_date_str(yt) -> str | None:
    try:
        cal = yt.calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, (list, tuple)) and ed:
                ed = ed[0]
            d = _coerce_date(ed)
            return d.isoformat() if d else None
    except Exception:  # noqa: BLE001
        pass
    return None


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:  # noqa: BLE001
        return None
