# Literature Digest

A small, self-hosted agent that reads the biomedical literature for you every
morning. Each day it pulls newly indexed papers and preprints, judges each one
against **your own research profile** with an LLM, and emails you a short digest
— the handful of papers actually worth your attention, topped with a few
sentences of context written by the model.

It is deliberately boring infrastructure: plain Python, a SQLite ledger, a cron
job, and a couple of text config files you edit to describe what you care about.
No database server, no web app to host, no vendor lock-in. Point it at your
field, let it run, and read your inbox.

---

## What it does

Once a day (via cron) the pipeline:

1. **Fetches** newly indexed records from **PubMed** (published papers) and
   **Europe PMC** (bioRxiv / medRxiv preprints) over a rolling multi-day window.
2. **Deduplicates** against a local SQLite ledger, so each paper is only ever
   considered — and sent — once, even across sources and across preprint →
   published transitions.
3. **Scores** every new candidate 0–10 for relevance to *your* research program,
   using a cheap LLM with your research profile as the rubric.
4. **Selects** the papers worth surfacing (a tight "Core" tier plus an optional
   "Watch" tier for borderline work in flagship journals).
5. **Writes a briefing** — a 3–5 sentence morning overview of the day's picks
   (the must-read, the themes, cross-paper links) with a more capable model.
6. **Delivers** the digest by email (and optionally Slack).

### Why it's built this way

A few design choices are worth understanding before you adapt it:

- **Broad prefilter, smart judge.** A cheap deterministic keyword query cuts the
  daily firehose down to a candidate set *before* any LLM tokens are spent. That
  query is intentionally kept broad — it's a one-way door — and the LLM does the
  fine discrimination. This keeps cost negligible while letting relevance
  judgments be nuanced.
- **Rolling window + ledger, not "yesterday's papers."** PubMed back-dates and
  late-indexes records, so a naive single-day query silently drops papers. The
  agent queries a multi-day window and relies on the ledger — not the date filter
  — to guarantee each paper is sent exactly once. The window auto-extends to cover
  any gap since the last successful run, so a missed day self-heals.
- **The research profile is the product.** Relevance is defined entirely by an
  editable plain-text file. No model retraining, no code changes — you edit prose
  and examples, re-run a backtest, and the behavior changes.
- **Cheap by construction.** Your (large) research profile is sent as a *cached*
  system block, so across a day's many per-paper calls only the small
  title + abstract is uncached. Scoring a full day of candidates costs well under
  a cent.
- **Auditable judgments.** Every score is stored with the model, a hash of the
  profile it was judged against, and a timestamp — so judgments stay comparable
  when you edit the profile or change models.

---

## Requirements

- A machine that stays on and can run cron (a lab server, workstation, or VM —
  Linux or macOS).
- **Python 3.11+**.
- A free **NCBI API key** (raises the PubMed rate limit; takes two minutes to get).
- An **Anthropic API key** for relevance scoring and the briefing.
- An outbound **email account** for delivery — a Gmail account with an *app
  password* is the path documented here, but any SMTP server works.
- (Optional) a **Slack incoming webhook** if you also want it posted to a channel.

---

## Setup

```bash
git clone https://github.com/dgorkin/literature-digest.git
cd literature-digest

python -m venv .venv && source .venv/bin/activate    # or use conda (see Scheduling)
pip install -r requirements.txt

cp .env.example .env                                        # then fill in secrets
cp config/research_profile.example.md config/research_profile.md   # then make it yours
```

Both `.env` and `config/research_profile.md` are gitignored — they hold your
secrets and your personal research program. The repo ships `.env.example` and
`research_profile.example.md` as templates.

### Fill in `.env`

Open `.env` and set:

- `NCBI_API_KEY` — register a free key at
  <https://www.ncbi.nlm.nih.gov/account/> → Settings → API Key Management. Also
  set `NCBI_EMAIL` to your contact email (NCBI asks callers to identify themselves).
- `ANTHROPIC_API_KEY` — from <https://console.anthropic.com/>.
- **Email delivery** — `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD` and
  `DIGEST_FROM`/`DIGEST_TO`. For Gmail, `SMTP_PASSWORD` must be a 16-character
  [app password](https://myaccount.google.com/apppasswords) (requires 2-Step
  Verification), **not** your normal password. `DIGEST_FROM` should match the
  relaying account or the mail may fail SPF/DKIM. `DIGEST_TO` can be a
  comma-separated list.

The remaining `.env` entries (Slack, the feedback web page) are optional and
documented inline in `.env.example`.

### Make the research profile your own

This is the important step — it's what teaches the agent your field. Edit
`config/research_profile.md` following the template's four sections:

- **(a) What you work on** — your program in prose: systems, molecules, methods,
  disease contexts. Be specific (gene/complex names, assays, model systems).
- **(b) What you want to see** — the kinds of papers that are a clear "keep."
- **(c) What you do *not* want** — adjacent-but-off-program near-misses. This is
  what separates signal from firehose; don't skip it.
- **(d) Concrete "would want" examples** — a handful of real paper titles you'd
  have wanted, each with a one-line why. These anchor the score scale far better
  than prose alone.

Write it the way you'd brief a new lab member on "what I'd want to see." The
model reads it verbatim.

### Point the search at your field

Edit `config/query.yaml`:

- **`pubmed_term`** — the broad boolean prefilter. Replace the example
  (epigenomics/chromatin vocabulary) with your field's terms. Keep it broad:
  anything excluded here the relevance judge never sees.
- **`europepmc_query`** — the preprint feed's query (Europe PMC syntax differs
  slightly from PubMed's; it's restricted to bioRxiv/medRxiv preprints by default).
- **`top_journals`** — flagship venues for the optional "Watch" tier.
- **Thresholds** — `relevance_threshold` (Core tier, any journal) and
  `broad_threshold` (Watch tier, top journals only). Set `broad_threshold` ≥
  `relevance_threshold` to disable the Watch tier entirely.

`config/digest_format.md` controls output formatting (grouping, ordering, tone)
and `config/negatives.json` holds hand-picked near-miss papers used only by the
backtest below. All config is plain text, reloaded every run — no code changes.

---

## Validate before you automate

Before wiring up cron, run a couple of manual passes to confirm it's behaving.

**Fetch + dedup only** (no LLM tokens spent — good first smoke test). Run it
twice; the second run should find 0 new candidates because the ledger already
has them:

```bash
python -m src.main --no-score      # N new candidates
python -m src.main --no-score      # 0 new candidates — dedup works
```

**Full run to stdout** (fetch + dedup + score + briefing, printed, no email):

```bash
python -m src.main
```

**Backtest your profile.** Score a set of papers you *know* are relevant against
your current profile to check recall, and your `negatives.json` to check
precision. Re-run this after every profile edit:

```bash
python -m src.backtest                                      # recall on your keeps
python -m src.backtest --negatives config/negatives.json    # + precision on near-misses
python -m src.backtest --limit 30                           # cheap subset while tuning
```

The goal: the large majority of your "keep" papers clear the threshold and the
near-misses fall below it. Adjust `research_profile.md` and the thresholds until
that holds.

> The backtest expects a JSON library of your own known-relevant papers. The
> maintainer's private library isn't shipped; supply your own set (or lean on
> `negatives.json` + a few `--limit` runs) when tuning.

**Send a real digest** once you're happy:

```bash
python -m src.main --send          # scores, selects, emails, and marks papers sent
```

`--send` only marks papers as sent on a clean delivery, and an empty day is
suppressed (no email). The first `--send` covers the full rolling-window backfill;
steady daily runs are much smaller.

---

## Scheduling (daily cron)

The repo includes a wrapper and a crontab template.

- **`run_daily.sh`** activates the Python environment, runs
  `python -m src.main --send`, and appends a dated log under `logs/`. It's written
  for a **conda** env named `lit-digest` — if you used a venv instead, edit the
  activation line near the top to source your `.venv` (or replace `conda run …`
  with a direct path to your interpreter). It resolves the project directory from
  its own location, so cron can call it by absolute path.
- **`crontab.example`** shows a Mon–Fri 7am schedule, pinned to US Eastern via
  `CRON_TZ`.

```bash
chmod +x run_daily.sh
crontab -e        # paste the line from crontab.example, with the real path
```

---

## Optional extras

- **Slack.** Set `SLACK_WEBHOOK_URL` in `.env` (create one at
  <https://api.slack.com/apps> → Incoming Webhooks). When set, `--send` also posts
  the digest to that channel; email stays the system of record.
- **Reader feedback (click-to-rate).** `src/webfeedback.py` runs a tiny
  localhost-only web page listing recently sent papers with graded rating buttons
  (miss / weak / good / core). Clicks write straight to the ledger. Reach it over
  an SSH tunnel from your laptop, then `python -m src.main --feedback-report`
  prints where you and the model disagree — useful raw material for tuning the
  profile. See the feedback section of `.env.example` for the tunnel command and
  settings.
- **Re-render a past digest.** `resend_email.py` / `resend_slack.py` re-render an
  already-sent date to one channel (e.g. to resend or to preview formatting
  changes).

---

## Cost

Scoring runs on a cheap model with your profile cached, so a typical day
(≈100–150 candidate papers) costs well under one cent. The once-a-day briefing
uses a more capable model over just the selected handful — a rounding error on
top. The dominant "cost" is the two minutes you spend getting an NCBI key.

---

## Project layout

```
config/
  query.yaml               # sources, rolling window, prefilter terms, thresholds, models
  research_profile.md       # what's relevant — your program (gitignored; copy the .example)
  digest_format.md          # output formatting only
  negatives.json            # near-miss papers for the backtest precision check
src/
  config.py  store.py  fetch_pubmed.py       # config, SQLite ledger, PubMed client
  fetch_europepmc.py                          # Europe PMC preprint feed
  relevance.py  journals.py                   # LLM relevance scoring + tier/journal matching
  briefing.py  digest.py                      # morning briefing + digest rendering
  deliver.py  slack.py                        # email (SMTP) + Slack delivery
  feedback.py  webfeedback.py                 # reader feedback scale + local rating page
  backtest.py                                 # profile validation harness
  main.py                                     # orchestrator (--no-score / --send / --feedback-report)
data/
  digest.sqlite             # the ledger (gitignored; created on first run)
resend_email.py  resend_slack.py             # re-render a past sent date to one channel
run_daily.sh  crontab.example                # daily cron wrapper + schedule template
```

---

## A note on adapting it

This started as one researcher's tool for tracking chromatin / epigenomics work,
so the shipped `query.yaml`, journal list, and profile template lean that way.
Nothing about the machinery is field-specific — swap in your own terms, journals,
and profile and it works for any corner of the biomedical literature indexed by
PubMed or posted to bioRxiv/medRxiv. If your field lives on a different preprint
server or database, the fetch modules (`src/fetch_*.py`) are small and
self-contained enough to fork.

## License

MIT — see [LICENSE](LICENSE).
