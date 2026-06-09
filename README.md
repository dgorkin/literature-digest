# Literature Digest Agent

A daily agent that scans newly indexed biomedical literature, judges relevance
against an editable research profile, and emails a digest. See
`../literature-digest-spec.md` for the full build brief.

**Status: Phase 2** — PubMed fetch + dedup ledger + LLM relevance scoring +
backtest harness + stdout digest. No email/cron yet (Phase 3).

## Setup

```bash
cd literature-digest
pip install -r requirements.txt
cp .env.example .env                                   # then edit .env (secrets)
cp config/research_profile.example.md config/research_profile.md   # then edit it
```

`.env` and `config/research_profile.md` are gitignored (secrets / personal
research program). The repo ships `.env.example` and `research_profile.example.md`
as templates — copy and fill them in before the first run.

Register a free NCBI API key (raises the rate limit from ~3 to ~10 req/s) at
<https://www.ncbi.nlm.nih.gov/account/> → API Key Management, and put it in
`.env` along with your contact email. For Phase 2 scoring you also need
`ANTHROPIC_API_KEY` in `.env`.

## Run

```bash
python -m src.main                # fetch + dedup + score + print digest
python -m src.main --no-score     # fetch + dedup only (Phase-1 behavior, no API key)
```

A full run queries PubMed for records that **entered** PubMed (Entrez date)
within the rolling window, normalizes them, drops anything already in the ledger,
stores the rest, **scores each new candidate against `config/research_profile.md`**
(writing score + rationale + provenance back to the DB), and prints a digest of
the papers at or above `relevance_threshold`. One `runs` row per execution.

### Proving dedup (Phase 1 acceptance)

Run it twice. The first run stores candidates; the second finds 0 new because the
SQLite ledger already has them (use `--no-score` to avoid spending API tokens):

```bash
python -m src.main --no-score      # N new candidates
python -m src.main --no-score      # 0 new candidates
```

## Backtest (Phase 2 acceptance)

Score the curated library (`reference_materials/papers_of_interest/library.json`)
against the current profile to choose/validate the threshold. All library papers
are keeps, so this measures **recall**; the hand-authored `config/negatives.json`
provides the only **precision** signal until live feedback exists.

```bash
python -m src.backtest                              # library only (recall)
python -m src.backtest --negatives config/negatives.json   # + precision check
python -m src.backtest --limit 30                   # cheap subset while tuning
```

Goal: the large majority of library papers clear the threshold and the negatives
fall below it. Re-run after any edit to `research_profile.md`.

## Relevance scoring details

- Each paper is scored 0–10 by a cheap model (`scoring_model` in `query.yaml`,
  default `claude-haiku-4-5`) via **forced tool-use** — the model must return
  structured `{score, matched_area, rationale}`, never free text.
- The large research profile is sent as a **cached** system block, so across a
  run's many per-paper calls only the small title+abstract is uncached.
- Every score stores its `scored_model`, `profile_hash`, and `scored_at` so
  judgments stay comparable across profile edits and model changes.
- A scoring failure degrades gracefully to score 0 (logged), never aborting the run.

## Why a rolling window + ledger (not "yesterday's papers")

PubMed back-dates and late-indexes records, so a single-day query silently drops
papers. We query a multi-day window (`config/query.yaml: lookback_days`) using
`datetype=edat`, and rely on the database — not the date filter — to guarantee
each paper is sent exactly once. The window auto-extends to cover any gap since
the last successful run. See spec §6–§7.

## Configuration (`config/`)

All config is plain text, reloaded every run — no code change needed.

- `query.yaml` — sources, rolling-window size, candidate cap, the boolean PubMed
  prefilter term, and scoring knobs (`relevance_threshold`, `scoring_model`,
  `digest_model`). Keep the prefilter term **broad**; it is a one-way door.
- `research_profile.md` — what counts as relevant: prose + real "would want"
  examples (from the library) + hand-authored "do not want" near-misses. Edit
  freely; re-backtest after changes.
- `digest_format.md` — output formatting only (grouping, ordering, length, tone).
- `negatives.json` — hand-authored near-miss papers for the backtest precision check.

## Why `requests` (not Biopython)

A thin direct E-utilities client keeps dependencies minimal and the request/parse
path explicit (we only need a narrow slice of the XML). Biopython's `Bio.Entrez`
is a fine alternative if richer parsing is wanted later.

## Layout

```
config/   query.yaml  research_profile.md  digest_format.md  negatives.json
src/      config.py  store.py  fetch_pubmed.py  relevance.py  digest.py  backtest.py  main.py
data/     digest.sqlite  (gitignored)
```
