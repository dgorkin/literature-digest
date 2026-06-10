"""Reader feedback core (spec §12, Phase 5).

Mechanism-agnostic pieces shared by whatever captures ratings: the graded rating
scale and the human-vs-model disagreement report. The actual capture is the
local web page in src/webfeedback.py (click a rating button -> store.record_feedback
writes it to the same ledger); this module deliberately knows nothing about how a
rating arrives.

Provenance: ratings are graded to a 0-10 score on the SAME scale the model uses
(LABEL_SCORES), so the report can line human vs. model up directly.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Graded ratings -> a point on the model's 0-10 scale. Ordered worst->best for
# rendering. Edit here to retune; the report and the web page both read this map.
LABEL_SCORES: dict[str, int] = {"miss": 2, "weak": 4, "good": 7, "core": 9}


def disagreement_report(conn, store, gap: int = 3, limit: int | None = None
                        ) -> tuple[str, dict]:
    """Compare each rated paper's human score (LABEL_SCORES) to the model's
    relevance_score. Returns (text, stats). A 'disagreement' is |human-model| >=
    gap — the cases worth eyeballing when tuning the profile/threshold."""
    rows = store.feedback_rows(conn, limit=limit)
    if not rows:
        return "No reader feedback recorded yet.", {"n": 0, "disagreements": 0}

    lines, disagreements, gaps = [], [], []
    for r in rows:
        human = LABEL_SCORES.get((r["feedback"] or "").lower())
        model = r["relevance_score"]
        if human is None or model is None:
            continue
        d = human - model
        gaps.append(abs(d))
        arrow = "model HIGH" if d < 0 else ("model LOW" if d > 0 else "agree")
        flag = "  <-- " + arrow if abs(d) >= gap else ""
        title = (r["title"] or "(no title)")[:62]
        lines.append(
            f"  human {human} ({r['feedback']:>4}) vs model {model:>4} "
            f"[{r['matched_area'] or '?'}] {title}{flag}")
        if abs(d) >= gap:
            disagreements.append(r)

    n = len(gaps)
    mae = sum(gaps) / n if n else 0.0
    header = (f"Feedback vs. model — {n} rated paper(s), "
              f"{len(disagreements)} disagreement(s) (gap >= {gap}), "
              f"mean abs error {mae:.1f}")
    text = header + "\n" + "\n".join(lines)
    return text, {"n": n, "disagreements": len(disagreements), "mae": mae}
