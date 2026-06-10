"""LLM morning briefing (spec §8 digest stage, "LLM synthesis layered on later").

A single call to the stronger digest_model writes a 2-3 sentence overview of the
papers ACTUALLY selected for today's digest: the one must-read, then a quick note
of what else is in today's batch. It sees only what the reader will see (title,
journal, area, score, rationale) — never papers below threshold — so it cannot
leak excluded work into the digest.

Same structured-output discipline as relevance.py: the model MUST reply through a
forced tool call, so we never parse free text. Failure here is cosmetic by design:
any error returns None and the digest renders without a briefing (spec §7.4-7.5
graceful degradation) — this stage must never block a send.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

BRIEFING_TOOL = {
    "name": "record_briefing",
    "description": (
        "Record the morning briefing for today's digest. You MUST call this "
        "exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "briefing": {
                "type": "string",
                "description": (
                    "2-3 sentences of plain prose (no markdown, no list). Open "
                    "with the day's single most important paper and its key "
                    "finding; then briefly note what else is in the batch "
                    "(areas, anything unusual). Do not construct a narrative "
                    "or force connections between papers. Terse, collegial, "
                    "no hype — a working scientist's morning scan. Do not "
                    "enumerate every paper."
                ),
            },
        },
        "required": ["briefing"],
    },
}

SYSTEM_INSTRUCTIONS = (
    "You write the opening briefing for a daily biomedical literature digest "
    "read by an epigenomics researcher (chromatin regulation, BAF/SWI/SNF, "
    "3D genome, enhancers, neurodevelopment, single-cell epigenomics). You are "
    "given today's selected papers with their relevance scores (0-10) and "
    "short summaries. Write 2-3 sentences: lead with the must-read and its "
    "key finding, then briefly note what else is in today's batch. Do not "
    "build a narrative or force connections between papers. Refer to papers "
    "by short natural descriptions (first author or topic), never by number. "
    "Plain prose only. Always respond by calling the record_briefing tool."
)

_MAX_PAPERS_IN_PROMPT = 60  # the digest cap is 50; headroom, never silent loss


def _paper_line(rec: dict, s) -> str:
    flags = []
    if rec.get("is_preprint"):
        flags.append("preprint")
    if rec.get("prior_preprint_sent_on"):
        flags.append("now published, preprint previously sent")
    flag = f" ({'; '.join(flags)})" if flags else ""
    title = (rec.get("title") or "(no title)").rstrip(".")
    authors = rec.get("authors") or ""
    first = authors.split(",")[0].strip() if authors.strip() else "?"
    return (f"- [{s.matched_area}] score {s.score}: {title}{flag} — "
            f"{first} et al., {rec.get('journal') or '?'}. Summary: {s.rationale}")


def write_briefing(selected, model: str, run_date: str, client=None,
                   max_tokens: int = 600) -> str | None:
    """One Sonnet-class call over the selected (rec, Score) pairs -> briefing
    prose, or None on any failure / empty input (digest renders without it)."""
    if not selected or not model:
        return None
    if client is None:
        import anthropic  # lazy, as in relevance.py
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            log.warning("ANTHROPIC_API_KEY not set — skipping briefing.")
            return None
        client = anthropic.Anthropic(api_key=api_key)

    lines = [_paper_line(rec, s) for rec, s in selected[:_MAX_PAPERS_IN_PROMPT]]
    if len(selected) > _MAX_PAPERS_IN_PROMPT:
        log.warning("Briefing prompt capped at %d of %d papers.",
                    _MAX_PAPERS_IN_PROMPT, len(selected))
    user = (f"Digest date: {run_date}. {len(selected)} selected paper(s), "
            f"highest-scored first:\n\n" + "\n".join(lines))

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_INSTRUCTIONS,
            tools=[BRIEFING_TOOL],
            tool_choice={"type": "tool", "name": "record_briefing"},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_briefing":
                text = (block.input.get("briefing") or "").strip()
                return text or None
        raise ValueError("no record_briefing tool_use in response")
    except Exception as exc:  # noqa: BLE001 — cosmetic stage, never block the send
        log.warning("briefing failed (digest proceeds without it): %s", exc)
        return None
