"""One-off: re-render the digest already sent on a given date and post it to
Slack ONLY (no email, no ledger writes). For visually checking Slack formatting.

Usage: conda run -n lit-digest python resend_slack.py [YYYY-MM-DD]
"""
from __future__ import annotations

import sqlite3
import sys

from src import config, store
from src import digest as digest_mod
from src.relevance import Score
from src.journals import build_matcher
from src import slack as slack_mod

run_date = sys.argv[1] if len(sys.argv) > 1 else "2026-06-09"

config.load_env()
cfg = config.load_query_config()
threshold = cfg.get("relevance_threshold", 6)
broad_threshold = cfg.get("broad_threshold")
offlist_threshold = cfg.get("offlist_threshold")
max_papers = cfg.get("digest_max_papers", 25)
is_top_journal = build_matcher(cfg.get("top_journals"))

conn = sqlite3.connect(config.get_db_path())
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT * FROM papers WHERE sent_on=? ", (run_date,)
).fetchall()

scored = []
for r in rows:
    rec = dict(r)
    s = Score(
        score=rec.get("relevance_score") or 0,
        matched_area=rec.get("matched_area") or "Other",
        rationale=rec.get("relevance_rationale") or "",
        model=rec.get("scored_model") or "",
        profile_hash=rec.get("profile_hash") or "",
        scored_at=rec.get("scored_at") or "",
    )
    scored.append((rec, s))

print(f"{len(scored)} papers sent on {run_date}; rendering to Slack...")

msgs = digest_mod.render_slack(
    scored, threshold, run_date,
    broad_threshold=broad_threshold, is_top_journal=is_top_journal,
    max_papers=max_papers, offlist_threshold=offlist_threshold)

print(f"render_slack produced {len(msgs)} message(s); posting...")
slack_mod.post_digest(msgs, config.slack_webhook())
print("posted OK")
