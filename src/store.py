"""SQLite store: dedup ledger + feedback/score record (spec §8).

The schema includes every column the later phases need (relevance scores,
provenance, send status, feedback) even though Phase 1 only populates the
fetch/dedup columns. Keeping the schema stable from the start avoids migrations.

Dedup contract (spec §7): a paper is "already seen" if its (source, source_id)
is present, OR — when it carries a DOI — if that DOI is already in the table.
The window provides coverage; this table provides uniqueness.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    doi                 TEXT,
    pmid                TEXT,
    title               TEXT,
    authors             TEXT,
    journal             TEXT,
    pub_date            TEXT,
    source_date         TEXT,
    abstract            TEXT,
    url                 TEXT,
    is_preprint         INTEGER NOT NULL DEFAULT 0,
    norm_title          TEXT,    -- normalized title, for preprint->published match
    first_seen          TEXT NOT NULL,
    -- relevance stage (Phase 2)
    relevance_score     REAL,
    relevance_rationale TEXT,
    matched_area        TEXT,
    scored_model        TEXT,
    profile_hash        TEXT,
    scored_at           TEXT,
    -- delivery + feedback (Phases 3 & 5)
    sent_on             TEXT,
    feedback            TEXT,
    feedback_on         TEXT,
    -- preprint->published re-surface (Phase 4, spec §7.6)
    is_republication       INTEGER NOT NULL DEFAULT 0,
    prior_preprint_sent_on TEXT,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_papers_doi  ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_pmid ON papers(pmid);
-- idx_papers_norm_title is created in _migrate, after the column is ALTERed in
-- on pre-existing ledgers (it can't reference the column before it exists here).

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL,
    window_start  TEXT,
    window_end    TEXT,
    n_candidates  INTEGER,
    n_scored      INTEGER,
    n_sent        INTEGER,
    status        TEXT,
    error         TEXT
);
"""

_INSERT_COLS = (
    "source", "source_id", "doi", "pmid", "title", "authors", "journal",
    "pub_date", "source_date", "abstract", "url", "is_preprint", "norm_title",
    "first_seen",
)

# Columns added after the first release, with their definitions, for in-place
# migration of existing ledgers (CREATE TABLE IF NOT EXISTS won't add them).
_MIGRATIONS = {
    "norm_title": "TEXT",
    "is_republication": "INTEGER NOT NULL DEFAULT 0",
    "prior_preprint_sent_on": "TEXT",
}


def norm_title(title: str | None) -> str | None:
    """Normalize a title for preprint->published matching: lowercase, keep only
    alphanumerics + single spaces. Returns None for empty/missing titles."""
    if not title:
        return None
    s = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return s or None


def connect(db_path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add post-release columns to a pre-existing papers table and backfill
    norm_title for rows that predate it. No-op on a freshly created schema."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    for col, ddl in _MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_papers_norm_title ON papers(norm_title)")
    # Backfill norm_title for legacy rows (e.g. the Phase-1/2 ledger).
    rows = conn.execute(
        "SELECT id, title FROM papers WHERE norm_title IS NULL AND title IS NOT NULL"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE papers SET norm_title=? WHERE id=?",
                     (norm_title(r["title"]), r["id"]))


def _seen_in_db(conn: sqlite3.Connection, rec: dict) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM papers WHERE source=? AND source_id=? LIMIT 1",
        (rec["source"], rec["source_id"]),
    )
    if cur.fetchone():
        return True
    doi = rec.get("doi")
    if doi:
        cur = conn.execute("SELECT 1 FROM papers WHERE doi=? LIMIT 1", (doi,))
        if cur.fetchone():
            return True
    return False


def filter_new(conn: sqlite3.Connection, records: Iterable[dict]) -> list[dict]:
    """Return records not already in the ledger, also de-duping within the batch.

    Within-batch dedup matters once a second source is added (same DOI arriving
    from PubMed and Europe PMC in one run); harmless with PubMed alone.
    """
    new: list[dict] = []
    seen_ids: set[tuple[str, str]] = set()
    seen_dois: set[str] = set()
    for rec in records:
        key = (rec["source"], rec["source_id"])
        doi = rec.get("doi")
        if key in seen_ids or (doi and doi in seen_dois):
            continue
        if _seen_in_db(conn, rec):
            continue
        new.append(rec)
        seen_ids.add(key)
        if doi:
            seen_dois.add(doi)
    return new


def insert_papers(conn: sqlite3.Connection, records: Iterable[dict]) -> int:
    """Insert candidate papers; returns the number actually inserted.

    Uses INSERT OR IGNORE against the UNIQUE(source, source_id) constraint so a
    concurrent/duplicate insert is a no-op rather than an error (idempotency).
    """
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    placeholders = ", ".join("?" for _ in _INSERT_COLS)
    sql = f"INSERT OR IGNORE INTO papers ({', '.join(_INSERT_COLS)}) VALUES ({placeholders})"
    for rec in records:
        row = (
            rec["source"], rec["source_id"], rec.get("doi"), rec.get("pmid"),
            rec.get("title"), rec.get("authors"), rec.get("journal"),
            rec.get("pub_date"), rec.get("source_date"), rec.get("abstract"),
            rec.get("url"), 1 if rec.get("is_preprint") else 0,
            norm_title(rec.get("title")), now,
        )
        cur = conn.execute(sql, row)
        inserted += cur.rowcount
    conn.commit()
    return inserted


def update_score(conn: sqlite3.Connection, source: str, source_id: str, score) -> None:
    """Persist a relevance Score (and provenance) back to the paper row (spec §8)."""
    conn.execute(
        """UPDATE papers
           SET relevance_score=?, relevance_rationale=?, matched_area=?,
               scored_model=?, profile_hash=?, scored_at=?
           WHERE source=? AND source_id=?""",
        (score.score, score.rationale, score.matched_area,
         score.model, score.profile_hash, score.scored_at, source, source_id),
    )


def mark_sent(conn: sqlite3.Connection, ids: list[tuple[str, str]], sent_on: str) -> None:
    """Set sent_on for the given (source, source_id) pairs (idempotent)."""
    conn.executemany(
        "UPDATE papers SET sent_on=? WHERE source=? AND source_id=? AND sent_on IS NULL",
        [(sent_on, s, sid) for s, sid in ids],
    )
    conn.commit()


_MIN_TITLE_LEN = 20  # guard against matching short/generic titles


def find_sent_preprint(conn: sqlite3.Connection, rec: dict) -> str | None:
    """If `rec` (a published, non-preprint candidate) is the same work as a
    preprint we ALREADY SENT, return that preprint's sent_on date; else None.

    Match is by normalized title (spec §7.6). Preprint↔published DOIs differ, so
    DOI dedup can't catch this. We only re-surface works the user actually saw,
    so we require sent_on IS NOT NULL on the matched preprint."""
    nt = norm_title(rec.get("title"))
    if not nt or len(nt) < _MIN_TITLE_LEN:
        return None
    cur = conn.execute(
        """SELECT sent_on FROM papers
           WHERE is_preprint=1 AND sent_on IS NOT NULL AND norm_title=?
           ORDER BY sent_on LIMIT 1""",
        (nt,),
    )
    row = cur.fetchone()
    return row["sent_on"] if row else None


def mark_republication(conn: sqlite3.Connection, source: str, source_id: str,
                       prior_sent_on: str) -> None:
    """Flag a paper as the published version of an already-sent preprint."""
    conn.execute(
        "UPDATE papers SET is_republication=1, prior_preprint_sent_on=? "
        "WHERE source=? AND source_id=?",
        (prior_sent_on, source, source_id),
    )
    conn.commit()


def record_feedback(conn: sqlite3.Connection, source: str, source_id: str,
                    feedback: str, feedback_on: str) -> int:
    """Record reader feedback for one paper (Phase 5). Latest rating wins —
    re-rating overwrites. Returns the number of rows updated (0 = no such paper,
    so the caller can warn about feedback for an unknown id)."""
    cur = conn.execute(
        "UPDATE papers SET feedback=?, feedback_on=? WHERE source=? AND source_id=?",
        (feedback, feedback_on, source, source_id),
    )
    conn.commit()
    return cur.rowcount


def sent_for_rating(conn: sqlite3.Connection, days: int = 14) -> list[sqlite3.Row]:
    """Papers sent within the last `days` (by sent_on), newest first then by
    score — the list the web feedback page shows, each with its current rating
    (feedback may be NULL = unrated)."""
    return conn.execute(
        """SELECT source, source_id, title, authors, journal, pub_date, url,
                  is_preprint, relevance_score, relevance_rationale, matched_area,
                  sent_on, feedback
           FROM papers
           WHERE sent_on IS NOT NULL AND sent_on >= date('now', ?)
           ORDER BY sent_on DESC, relevance_score DESC""",
        (f"-{int(days)} days",),
    ).fetchall()


def feedback_rows(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Papers that have reader feedback, most recent first — for the disagreement
    report (model score vs. human rating)."""
    sql = ("SELECT source, source_id, title, journal, relevance_score, "
           "matched_area, feedback, feedback_on FROM papers "
           "WHERE feedback IS NOT NULL ORDER BY feedback_on DESC, id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def record_run(
    conn: sqlite3.Connection,
    *,
    run_at: str,
    window_start: str,
    window_end: str,
    n_candidates: int,
    n_scored: int,
    n_sent: int,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO runs
           (run_at, window_start, window_end, n_candidates, n_scored, n_sent, status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_at, window_start, window_end, n_candidates, n_scored, n_sent, status, error),
    )
    conn.commit()


def last_successful_window_end(conn: sqlite3.Connection) -> str | None:
    """Most recent window_end among successful runs, for the self-healing window
    (spec §7.2). None on first run."""
    cur = conn.execute(
        "SELECT window_end FROM runs WHERE status='ok' ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    return row["window_end"] if row else None
