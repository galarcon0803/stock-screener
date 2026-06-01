"""Sentiment classification of Reddit mentions via the Claude API.

Each mention is labeled (bullish_dd / bullish_general / bullish_fomo / neutral /
question / bearish) and given a one-line summary. Calls are batched and the
(static) system prompt is marked for prompt caching to keep cost low.
"""

from __future__ import annotations

import json
import logging
import re

import config

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a financial-text classifier analyzing Reddit posts \
and comments about stocks. You assess the *signal quality* of each mention for \
research purposes. You do NOT give investment advice.

For each item, assign exactly one label:
- bullish_dd: analytical bull case citing specific data (financials, catalysts, \
valuation, filings, charts with reasoning). High-quality positive signal.
- bullish_general: positive/optimistic but without specific supporting data.
- bullish_fomo: hype/FOMO-driven ("to the moon", rockets, "all in", squeeze \
hype) with little substance. Treat as a weak/pump signal.
- bearish: negative or skeptical on the stock.
- neutral: mentions the ticker without a directional view.
- question: asking about the stock rather than making a case.

Also write a concise one_line_summary (max ~15 words) capturing the take.

Return ONLY a JSON array, no prose. Each element:
{"id": <int>, "label": "<one of the labels>", "summary": "<one line>"}
Return one element per input item, preserving the given ids."""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def analyze_batch(mentions: list[dict]) -> list[dict]:
    """Annotate each mention in-place with sentiment_label/score/summary.

    Processes in batches of config.SENTIMENT_BATCH_SIZE. On any API/parse
    failure for a batch, those mentions fall back to a neutral label so the
    pipeline always proceeds.
    """
    if not mentions:
        return mentions

    client = _get_client()
    for start in range(0, len(mentions), config.SENTIMENT_BATCH_SIZE):
        batch = mentions[start : start + config.SENTIMENT_BATCH_SIZE]
        try:
            results = _classify(client, batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("Sentiment batch %d failed (%s); using neutral fallback",
                        start // config.SENTIMENT_BATCH_SIZE, exc)
            results = {}
        _apply(batch, results)
    labeled = sum(1 for m in mentions if m.get("sentiment_label"))
    log.info("Sentiment applied to %d/%d mentions", labeled, len(mentions))
    return mentions


def build_sentiment_prompt(mentions: list[dict]) -> str:
    """Build the user message: a compact, id-indexed list of items."""
    items = []
    for i, m in enumerate(mentions):
        text = (m.get("content_snippet") or m.get("post_title") or "").strip()
        text = re.sub(r"\s+", " ", text)[: config.CONTENT_SNIPPET_LEN]
        items.append(
            f'[{i}] ticker={m["ticker"]} subreddit={m["subreddit"]} '
            f'type={m.get("mention_type")} text="{text}"'
        )
    body = "\n".join(items)
    return (
        f"Classify these {len(mentions)} stock mentions. "
        f"Return a JSON array of {len(mentions)} objects with ids 0..{len(mentions)-1}.\n\n"
        f"{body}"
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _get_client():
    import anthropic

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _classify(client, batch: list[dict]) -> dict[int, dict]:
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.SENTIMENT_MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Cache the static instructions across batches/runs.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": build_sentiment_prompt(batch)}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    parsed = _parse_json_array(text)
    return {int(item["id"]): item for item in parsed if "id" in item}


def _apply(batch: list[dict], results: dict[int, dict]) -> None:
    for i, m in enumerate(batch):
        item = results.get(i)
        label = (item or {}).get("label", "neutral")
        if label not in config.VALID_SENTIMENT_LABELS:
            label = "neutral"
        m["sentiment_label"] = label
        m["sentiment_score"] = config.SENTIMENT_SCORES[label]
        m["sentiment_summary"] = (item or {}).get("summary", "")


def _parse_json_array(text: str) -> list[dict]:
    """Extract the first JSON array from the model output, tolerating fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("Could not parse JSON array from model response")
