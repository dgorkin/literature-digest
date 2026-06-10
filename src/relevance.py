"""LLM relevance scoring (spec §8, §9).

Each candidate is scored against config/research_profile.md by a cheap model that
MUST reply through a forced tool call (structured output), so we never parse free
text. The large, static research profile is sent as a cached system block, so
across a day's worth of per-paper calls only the small per-paper title+abstract
is uncached — cutting cost dramatically (prompt caching).

Provenance (model + profile hash + timestamp) is returned with every score so
the feedback loop can tell which profile/model produced a judgment (spec §8).

Model IDs are read from config and verified current; for Claude 4.6+ the dateless
alias is itself a pinned snapshot. Do not hardcode a stale string elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Tags the judge may assign (the §2b library vocabulary). "Other" is a safety
# valve so the model is never forced into a bad fit.
MATCHED_AREAS = [
    "BAF", "3D genome", "Enhancers", "NDD/Neuro", "single-cell",
    "Methods", "Epigenomics", "Other",
]

SCORE_TOOL = {
    "name": "record_relevance",
    "description": (
        "Record the relevance judgment for one paper against the research "
        "profile. You MUST call this exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 10,
                "description": (
                    "Relevance 0-10. ~9-10: squarely in the program (e.g. like a "
                    "paper already in the maintainer's library). ~6-8: clearly "
                    "relevant/adjacent. ~4-5: borderline. ~1-3: off-program "
                    "near-miss noise. 0: irrelevant."
                ),
            },
            "matched_area": {
                "type": "string",
                "enum": MATCHED_AREAS,
                "description": "The single best-fitting interest area.",
            },
            "rationale": {
                "type": "string",
                "description": (
                    "1-2 sentences (<=350 chars): the paper's main finding or "
                    "contribution, then how it relates to the research program. "
                    "Summarize the science, not the selection — do not explain "
                    "or justify the score."
                ),
            },
        },
        "required": ["score", "matched_area", "rationale"],
    },
}

SYSTEM_INSTRUCTIONS = (
    "You are a relevance judge for a biomedical literature digest. Score each "
    "paper STRICTLY against the research profile below. Reward work that is "
    "mechanistically in-program; penalize keyword-only near-misses (e.g. a "
    "cancer-metabolism paper that mentions a histone mark only as a readout). "
    "Always respond by calling the record_relevance tool.\n\n"
    "=== RESEARCH PROFILE ===\n"
)


def profile_hash(profile_text: str) -> str:
    return hashlib.sha256(profile_text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Score:
    score: int
    matched_area: str
    rationale: str
    model: str
    profile_hash: str
    scored_at: str
    ok: bool = True  # False => parse/call failure, treated as score 0 (spec §8)


def _candidate_text(rec: dict) -> str:
    title = rec.get("title") or "(no title)"
    abstract = rec.get("abstract") or "(no abstract available)"
    journal = rec.get("journal") or "?"
    return f"Journal: {journal}\nTitle: {title}\n\nAbstract:\n{abstract}"


class RelevanceJudge:
    """Scores candidates one at a time with a cached profile system block."""

    def __init__(self, profile_text: str, model: str, client=None, max_tokens: int = 400):
        self.profile_text = profile_text
        self.model = model
        self.max_tokens = max_tokens
        self.phash = profile_hash(profile_text)
        if client is None:
            import anthropic  # imported lazily so non-LLM phases need no SDK/key
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set — required for scoring.")
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client

    def _system_blocks(self) -> list[dict]:
        # The whole profile is static across the run -> cache it once. The 5-min
        # cache TTL easily covers a daily batch of dozens-to-hundreds of calls.
        return [
            {
                "type": "text",
                "text": SYSTEM_INSTRUCTIONS + self.profile_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def score_one(self, rec: dict) -> Score:
        now = datetime.now(timezone.utc).isoformat()
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self._system_blocks(),
                tools=[SCORE_TOOL],
                tool_choice={"type": "tool", "name": "record_relevance"},
                messages=[{"role": "user", "content": _candidate_text(rec)}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "record_relevance":
                    inp = block.input
                    return Score(
                        score=int(inp["score"]),
                        matched_area=inp.get("matched_area", "Other"),
                        rationale=(inp.get("rationale") or "").strip()[:450],
                        model=self.model, profile_hash=self.phash, scored_at=now,
                    )
            raise ValueError("no record_relevance tool_use in response")
        except Exception as exc:  # noqa: BLE001 — graceful degradation (spec §7.4)
            log.warning("scoring failed for %s: %s", rec.get("source_id"), exc)
            return Score(0, "Other", f"scoring error: {exc}", self.model,
                         self.phash, now, ok=False)


def score_candidates(records: list[dict], profile_text: str, model: str,
                     client=None) -> list[tuple[dict, Score]]:
    """Score each record; returns (record, Score) pairs in input order."""
    judge = RelevanceJudge(profile_text, model, client=client)
    out = []
    for i, rec in enumerate(records, 1):
        s = judge.score_one(rec)
        log.info("[%d/%d] %s -> %d (%s)", i, len(records),
                 (rec.get("title") or "")[:48], s.score, s.matched_area)
        out.append((rec, s))
    return out
