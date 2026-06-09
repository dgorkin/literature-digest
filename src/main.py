"""Daily run orchestrator.

Pipeline (spec §11.2-11.3): fetch (PubMed) -> normalize -> dedup against the
ledger -> insert -> score new candidates against the research profile -> write
scores+provenance back -> render the two-tier digest -> (optionally) email it and
mark papers sent -> write a runs row.

Run from the project root:
  python -m src.main                 # fetch + score + PRINT digest (no email)
  python -m src.main --send          # also email the digest (needs SMTP_* in .env)
  python -m src.main --no-score      # Phase-1 behavior: fetch + dedup only
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import config, store
from src.fetch_europepmc import EuropePMCClient
from src.fetch_pubmed import PubMedClient

log = logging.getLogger("lit_digest")


def compute_window(conn, lookback_days: int, today: date | None = None) -> tuple[date, date]:
    """Rolling window [start, end], extended back to cover any gap since the last
    successful run so a missed cron day can't drop papers (spec §7.1-7.2). Dedup
    makes the wider window safe — re-seen papers are ignored."""
    today = today or datetime.now(timezone.utc).date()
    window_end = today
    window_start = today - timedelta(days=lookback_days)
    last_end = store.last_successful_window_end(conn)
    if last_end:
        last = date.fromisoformat(last_end)
        if last < window_start:
            log.info("Extending window back to last successful run end %s", last)
            window_start = last
    return window_start, window_end


def fetch_pubmed_candidates(cfg: dict, window_start: date, window_end: date) -> list[dict]:
    ncbi = config.ncbi_settings()
    if not ncbi["api_key"]:
        log.warning("No NCBI_API_KEY set — limited to ~3 req/s. Register a free key.")
    client = PubMedClient(api_key=ncbi["api_key"], tool=ncbi["tool"], email=ncbi["email"])
    pmids = client.esearch(
        cfg["pubmed_term"], window_start, window_end,
        max_results=cfg.get("max_candidates_per_run", 400),
    )
    log.info("esearch -> %d PMIDs; fetching records", len(pmids))
    return client.fetch_records(pmids)


def fetch_europepmc_candidates(cfg: dict, window_start: date, window_end: date) -> list[dict]:
    client = EuropePMCClient()
    return client.search(
        cfg["europepmc_query"], window_start, window_end,
        max_results=cfg.get("max_candidates_per_run", 400),
    )


def print_candidates(records: list[dict]) -> None:
    if not records:
        print("\nNo new candidates this run.\n")
        return
    print(f"\n{'='*78}\n{len(records)} NEW CANDIDATE(S)\n{'='*78}")
    for i, r in enumerate(records, 1):
        flag = " [PREPRINT]" if r.get("is_preprint") else ""
        print(f"\n[{i}] {r.get('title') or '(no title)'}{flag}")
        print(f"    {r.get('journal') or '?'} · {r.get('pub_date') or '?'} · "
              f"edat {r.get('source_date') or '?'}")
        print(f"    {r.get('authors') or '(no authors)'}")
        print(f"    PMID {r.get('pmid')} · DOI {r.get('doi') or '—'}")
        print(f"    {r.get('url')}")
        abs = r.get("abstract")
        if abs:
            snippet = abs.replace("\n", " ")
            print(f"    {snippet[:240]}{'…' if len(snippet) > 240 else ''}")
    print()


def run(sources_override: list[str] | None = None, do_score: bool = True,
        send: bool = False) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config.load_env()
    cfg = config.load_query_config()
    sources = sources_override or cfg.get("sources", ["pubmed"])

    conn = store.connect(config.get_db_path())
    store.init_schema(conn)

    window_start, window_end = compute_window(conn, cfg.get("lookback_days", 5))
    run_at = datetime.now(timezone.utc).isoformat()
    log.info("Run window: %s .. %s | sources=%s", window_start, window_end, sources)

    candidates: list[dict] = []
    status, errors = "ok", []

    # Graceful degradation (spec §7.5): a failing source is logged, not fatal —
    # we proceed with whatever other sources returned.
    fetchers = {"pubmed": fetch_pubmed_candidates, "europepmc": fetch_europepmc_candidates}
    for src in sources:
        fetch = fetchers.get(src)
        if fetch is None:
            log.info("Source %r not implemented — skipping.", src)
            continue
        try:
            got = fetch(cfg, window_start, window_end)
            log.info("%s -> %d candidates", src, len(got))
            candidates += got
        except Exception as exc:  # noqa: BLE001 — surface, don't abort the run
            status, errors = "error", errors + [f"{src}: {exc}"]
            log.exception("%s fetch failed", src)

    new = store.filter_new(conn, candidates)
    inserted = store.insert_papers(conn, new)

    # Preprint -> published re-surface (spec §7.6): tag any new PUBLISHED paper
    # that is the same work as a preprint we already sent, so the digest can
    # re-surface it labeled "now published" (DOI dedup can't catch this).
    for rec in new:
        if rec.get("is_preprint"):
            continue
        prior = store.find_sent_preprint(conn, rec)
        if prior:
            rec["is_republication"] = True
            rec["prior_preprint_sent_on"] = prior
            store.mark_republication(conn, rec["source"], rec["source_id"], prior)
            log.info("Republication: %r now published (preprint sent %s)",
                     (rec.get("title") or "")[:60], prior)

    n_scored = 0
    n_sent = 0
    if do_score and new:
        from src.relevance import score_candidates
        from src import digest as digest_mod

        from src.journals import build_matcher

        profile = Path(config.CONFIG_DIR / "research_profile.md").read_text()
        model = cfg.get("scoring_model", "claude-haiku-4-5")
        threshold = cfg.get("relevance_threshold", 6)
        broad_threshold = cfg.get("broad_threshold")
        max_papers = cfg.get("digest_max_papers", 25)
        is_top_journal = build_matcher(cfg.get("top_journals"))
        try:
            scored = score_candidates(new, profile, model)
            for rec, s in scored:
                store.update_score(conn, rec["source"], rec["source_id"], s)
            conn.commit()
            n_scored = sum(1 for _, s in scored if s.ok)

            text, n_digest = digest_mod.render_plaintext(
                scored, threshold, window_end.isoformat(),
                broad_threshold=broad_threshold, is_top_journal=is_top_journal,
                max_papers=max_papers)
            if n_digest:
                print("\n" + text)
            else:
                print(f"\nNo papers >= threshold {threshold}; digest empty (would send nothing).\n")

            # Empty-day suppression (spec §11.3): only deliver when there's content.
            if send and n_digest:
                from src import deliver, slack as slack_mod

                subject = f"Literature digest — {window_end.isoformat()} ({n_digest} papers)"
                delivered_ok = False

                # Email — the system of record for sent-status.
                smtp = config.smtp_settings()
                email_configured = not deliver.validate(smtp)
                email_done = False
                if email_configured:
                    try:
                        html, _ = digest_mod.render_html(
                            scored, threshold, window_end.isoformat(),
                            broad_threshold=broad_threshold,
                            is_top_journal=is_top_journal, max_papers=max_papers)
                        deliver.send_digest(subject=subject, text_body=text,
                                            html_body=html, smtp=smtp)
                        email_done = delivered_ok = True
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"email: {exc}")
                        log.exception("Email delivery failed")
                else:
                    log.info("SMTP not configured — skipping email.")

                # Slack — additional channel. Skipped on email failure so a retry
                # of the run doesn't double-post to Slack.
                webhook = config.slack_webhook()
                if webhook and (email_done or not email_configured):
                    try:
                        msgs = digest_mod.render_slack(
                            scored, threshold, window_end.isoformat(),
                            broad_threshold=broad_threshold,
                            is_top_journal=is_top_journal, max_papers=max_papers)
                        slack_mod.post_digest(msgs, webhook)
                        delivered_ok = True
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"slack: {exc}")
                        log.exception("Slack delivery failed")

                # Mark sent once the digest reached at least one channel.
                if delivered_ok:
                    delivered = digest_mod.selected_papers(
                        scored, threshold, broad_threshold, is_top_journal, max_papers)
                    store.mark_sent(
                        conn,
                        [(rec["source"], rec["source_id"]) for rec, _ in delivered],
                        sent_on=date.today().isoformat(),
                    )
                    n_sent = len(delivered)
                if errors:
                    status = "error"
            elif send:
                log.info("Empty digest — nothing to send (suppressed).")
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            status = "error"
            errors.append(f"scoring/digest: {exc}")
            log.exception("Scoring/digest stage failed")
    else:
        # Phase-1 behavior: just show what was fetched.
        print_candidates(new)

    store.record_run(
        conn, run_at=run_at,
        window_start=window_start.isoformat(), window_end=window_end.isoformat(),
        n_candidates=len(candidates), n_scored=n_scored, n_sent=n_sent,
        status=status, error="; ".join(errors) or None,
    )
    log.info("fetched=%d new=%d inserted=%d scored=%d status=%s",
             len(candidates), len(new), inserted, n_scored, status)
    return 0 if status == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Literature digest — daily run (Phase 2)")
    parser.add_argument("--sources", nargs="+", help="override config sources (e.g. pubmed)")
    parser.add_argument("--no-score", action="store_true",
                        help="fetch + dedup only; skip LLM scoring/digest (Phase-1 behavior)")
    parser.add_argument("--send", action="store_true",
                        help="email the digest and mark papers sent (needs SMTP_* in .env). "
                             "Without it, the digest only prints to stdout.")
    args = parser.parse_args()
    return run(sources_override=args.sources, do_score=not args.no_score, send=args.send)


if __name__ == "__main__":
    raise SystemExit(main())
