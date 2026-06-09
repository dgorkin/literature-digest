"""Offline eval harness (spec §8 backtest stage).

Scores the maintainer's curated library against the current research profile and
reports the score distribution. ALL library papers are keeps, so this measures
RECALL (do known positives clear the threshold?), not precision. Optionally also
scores a hand-authored set of near-miss NEGATIVES, which is the only precision
signal available before live feedback exists.

Run from the project root:
  python -m src.backtest                         # library only (recall)
  python -m src.backtest --negatives config/negatives.json
  python -m src.backtest --limit 30              # quick/cheap subset

The library file is the export from the maintainer's Literature Manager app.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

from src import config
from src.relevance import RelevanceJudge

log = logging.getLogger("backtest")

DEFAULT_LIBRARY = (
    config.PROJECT_ROOT.parent
    / "reference_materials" / "papers_of_interest" / "library.json"
)


def load_library(path: Path, limit: int | None = None) -> list[dict]:
    """Load library papers (those with an abstract) as scoring records."""
    data = json.loads(Path(path).read_text())
    tag = {t["id"]: t["name"] for t in data.get("tags", [])}
    recs = []
    for p in data["papers"]:
        if not p.get("abstract"):
            continue
        recs.append({
            "source": "library", "source_id": str(p["id"]),
            "title": p.get("title"), "abstract": p.get("abstract"),
            "journal": p.get("journal"), "is_preprint": False,
            "_priority": p.get("priority"),
            "_tags": [tag.get(t) for t in p.get("tag_ids", [])],
        })
    return recs[:limit] if limit else recs


def load_negatives(path: Path, limit: int | None = None) -> list[dict]:
    """Hand-authored near-miss negatives. JSON list of {title, abstract, journal}."""
    items = json.loads(Path(path).read_text())
    recs = [{
        "source": "negative", "source_id": f"neg-{i}",
        "title": it.get("title"), "abstract": it.get("abstract"),
        "journal": it.get("journal", "?"), "is_preprint": False,
        "_priority": None, "_tags": ["(negative)"],
    } for i, it in enumerate(items)]
    return recs[:limit] if limit else recs


def _hist(scores: list[int]) -> str:
    buckets = defaultdict(int)
    for s in scores:
        buckets[s] += 1
    rows = []
    for s in range(10, -1, -1):
        if buckets[s]:
            rows.append(f"    {s:>2}: {'#' * buckets[s]} ({buckets[s]})")
    return "\n".join(rows)


def report(results: list[tuple[dict, "object"]], threshold: int, label: str,
           expect_pass: bool) -> dict:
    scores = [s.score for _, s in results]
    n = len(scores)
    n_pass = sum(1 for x in scores if x >= threshold)
    rate = (n_pass / n * 100) if n else 0.0
    direction = "clearing" if expect_pass else "(should stay) below"
    print(f"\n{'='*64}\n{label}: {n} papers | threshold={threshold}")
    print(f"  {n_pass}/{n} ({rate:.0f}%) {direction} threshold")
    print(f"  mean score: {sum(scores)/n:.1f}" if n else "  (no papers)")
    print("  score distribution:")
    print(_hist(scores))

    # Per-tag pass rate (library only) — surfaces blind spots in the profile.
    by_tag = defaultdict(lambda: [0, 0])
    for rec, s in results:
        for t in rec.get("_tags") or ["(untagged)"]:
            by_tag[t][0] += 1
            if s.score >= threshold:
                by_tag[t][1] += 1
    if len(by_tag) > 1:
        print("  per-tag pass rate:")
        for t, (tot, passed) in sorted(by_tag.items(), key=lambda x: -x[1][0]):
            print(f"    {t:<16} {passed:>3}/{tot:<3} ({passed/tot*100:.0f}%)")

    # The interesting failures to eyeball.
    misses = [(rec, s) for rec, s in results
              if (s.score < threshold) == expect_pass]
    if misses:
        tag = "library papers BELOW threshold (gaps?)" if expect_pass else \
              "negatives ABOVE threshold (false positives)"
        print(f"  {len(misses)} {tag}:")
        for rec, s in sorted(misses, key=lambda rs: rs[1].score)[:12]:
            print(f"    [{s.score}] {(rec.get('title') or '')[:64]} — {s.rationale[:60]}")
    return {"n": n, "n_pass": n_pass, "rate": rate}


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Backtest the relevance judge")
    ap.add_argument("--library", default=str(DEFAULT_LIBRARY))
    ap.add_argument("--negatives", help="path to hand-authored negatives JSON")
    ap.add_argument("--limit", type=int, help="score only the first N (cheap run)")
    ap.add_argument("--threshold", type=int)
    args = ap.parse_args()

    config.load_env()
    cfg = config.load_query_config()
    threshold = args.threshold if args.threshold is not None else cfg.get("relevance_threshold", 6)
    profile = Path(config.CONFIG_DIR / "research_profile.md").read_text()
    model = cfg.get("scoring_model", "claude-haiku-4-5")

    judge = RelevanceJudge(profile, model)
    print(f"Backtest | model={model} | profile_hash={judge.phash}")

    lib = load_library(Path(args.library), args.limit)
    print(f"Scoring {len(lib)} library papers…")
    lib_results = [(r, judge.score_one(r)) for r in lib]
    report(lib_results, threshold, "LIBRARY (keeps — expect PASS)", expect_pass=True)

    if args.negatives:
        negs = load_negatives(Path(args.negatives), args.limit)
        print(f"\nScoring {len(negs)} negatives…")
        neg_results = [(r, judge.score_one(r)) for r in negs]
        report(neg_results, threshold, "NEGATIVES (expect BELOW)", expect_pass=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
