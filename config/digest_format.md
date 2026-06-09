# Digest Format

Output-formatting instructions only — this governs how the digest looks, not what
counts as relevant (that is research_profile.md). Reloaded every run; edit freely.

## Grouping & ordering
- Group papers by `matched_area`. Stored enum values map to these display headings,
  shown in this order: BAF, Neuro/NDD, Enhancer biology, 3D Genome,
  Single cell 'omics, General Epigenomics, New methods, Other. Omit empty groups.
  (The stored enum keys remain BAF / NDD/Neuro / Enhancers / 3D genome / single-cell
  / Epigenomics / Methods / Other; only the headings and order differ — see
  digest.GROUP_ORDER / GROUP_LABELS.)
- Within a group, order by relevance score descending.

## Per-paper entry
- One line: **Title** (linked to the URL), then journal · date.
- Authors: first author et al. (full list only if 3 or fewer).
- A single italic line: *Why this matters to you* — one sentence, grounded in the
  paper's relevance to the profile (use the stored rationale as a starting point).
- Mark preprints with a `[preprint]` tag after the title.
- Do not include the full abstract.

## Limits & tone
- Max 25 papers in the digest; if more clear threshold, keep the highest-scoring
  25 and note how many were omitted.
- Subject line: "Literature digest — {date} ({N} papers)".
- Tone: terse, collegial, no hype. This is a working scientist's morning scan.
