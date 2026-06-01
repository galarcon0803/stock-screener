"""Render the daily HTML email from scored tickers (Jinja2)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import config

log = logging.getLogger(__name__)


def _env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(config.TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["pct"] = _fmt_pct
    env.filters["money"] = _fmt_money
    env.filters["big"] = _fmt_big
    env.filters["num"] = _fmt_num
    return env


def generate_report(scored_tickers: list[dict], run_date: str | None = None) -> str:
    """Return the full HTML report string for the given scored tickers."""
    run_date = run_date or date.today().isoformat()
    by_tier = {"ACT": [], "WATCH": [], "MONITOR": [], "NOISE": []}
    for t in scored_tickers:
        by_tier.setdefault(t.get("tier", "NOISE"), []).append(t)
    for tier in by_tier:
        by_tier[tier].sort(key=lambda x: x.get("conviction_score", 0), reverse=True)

    next_run = _next_business_day(run_date)
    context = {
        "run_date": run_date,
        "next_run": next_run,
        "total_scanned": len(scored_tickers),
        "counts": {tier: len(rows) for tier, rows in by_tier.items()},
        "act": by_tier["ACT"],
        "watch": by_tier["WATCH"],
        "monitor": by_tier["MONITOR"],
    }
    html = _env().get_template("report.html").render(**context)
    log.info("Report generated: %d ACT, %d WATCH, %d MONITOR",
             context["counts"]["ACT"], context["counts"]["WATCH"],
             context["counts"]["MONITOR"])
    return html


# --------------------------------------------------------------------------- #
# Jinja filters
# --------------------------------------------------------------------------- #
def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def _fmt_money(value) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _fmt_num(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _fmt_big(value) -> str:
    if value is None:
        return "—"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= div:
            return f"${value/div:.1f}{unit}"
    return f"${value:,.0f}"


def _next_business_day(run_date: str) -> str:
    d = date.fromisoformat(run_date)
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # Sat/Sun
        nxt += timedelta(days=1)
    return nxt.isoformat()
