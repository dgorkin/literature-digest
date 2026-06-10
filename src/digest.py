"""Build and render the digest (spec §8 digest stage).

Phase 2 renders a plaintext digest to stdout, grouped per config/digest_format.md.
The format file is advisory text for the structure; Phase 2 implements the core of
it deterministically (grouping, ordering, per-paper line, the 25-paper cap). The
stored per-paper rationale is shown directly, which keeps the per-paper rendering
cheap and fully offline-testable. The one LLM-written element is the optional
morning briefing (src/briefing.py) passed in by the caller — renderers only place
it; with briefing=None the digest is identical to the pre-briefing output.
"""
from __future__ import annotations

import html
import textwrap
from typing import Callable

# Display order for matched_area groups (mirrors config/digest_format.md). Keys are
# the stored matched_area enum values (relevance.MATCHED_AREAS); GROUP_LABELS maps
# each to the human-facing heading shown in the digest. Stored values are left
# untouched so historical rows and the scoring enum stay stable — only display
# order and labels change here.
GROUP_ORDER = [
    "BAF", "NDD/Neuro", "Enhancers", "3D genome",
    "single-cell", "Epigenomics", "Methods", "Other",
]
GROUP_LABELS = {
    "BAF": "BAF",
    "NDD/Neuro": "Neuro/NDD",
    "Enhancers": "Enhancer biology",
    "3D genome": "3D Genome",
    "single-cell": "Single cell 'omics",
    "Epigenomics": "General Epigenomics",
    "Methods": "New methods",
    "Other": "Other",
}
MAX_PAPERS = 25


def _no_journal(_journal):  # default matcher: broad tier disabled
    return False


def select_tiers(scored, tight_threshold: int, broad_threshold: int | None = None,
                 is_top_journal: Callable[[str | None], bool] = _no_journal,
                 max_papers: int = MAX_PAPERS):
    """Two-tier selection (see query.yaml). A paper is included if:
      - Tier A "Core":  score >= tight_threshold        (any journal), OR
      - Tier B "Watch": score >= broad_threshold AND journal is a top journal.
    Tier B excludes anything already in Tier A. All Tier-A scores exceed all
    Tier-B scores (broad_threshold < tight_threshold), so a single score-desc cap
    drops the weakest Tier-B papers first. Returns
    (core_groups, watch_groups, n_omitted)."""
    if broad_threshold is None or broad_threshold >= tight_threshold:
        broad_threshold = tight_threshold  # broad tier off -> behaves single-tier

    core, watch = [], []
    for rec, s in scored:
        # A republication is a work the user already received as a preprint and
        # asked to see again on publication — include it regardless of re-score.
        if s.score >= tight_threshold or rec.get("is_republication"):
            core.append((rec, s))
        elif s.score >= broad_threshold and is_top_journal(rec.get("journal")):
            watch.append((rec, s))

    core.sort(key=lambda rs: rs[1].score, reverse=True)
    watch.sort(key=lambda rs: rs[1].score, reverse=True)

    combined = core + watch  # core scores all >= tight > watch scores: already ordered
    n_omitted = max(0, len(combined) - max_papers)
    kept = combined[:max_papers]
    kept_core = [x for x in kept if x[1].score >= tight_threshold]
    kept_watch = [x for x in kept if x[1].score < tight_threshold]
    return _group(kept_core), _group(kept_watch), n_omitted


def _group(items) -> dict:
    groups: dict[str, list] = {}
    for rec, s in items:
        groups.setdefault(s.matched_area, []).append((rec, s))
    return groups


def selected_papers(scored, tight_threshold: int, broad_threshold: int | None = None,
                    is_top_journal: Callable[[str | None], bool] = _no_journal,
                    max_papers: int = MAX_PAPERS):
    """Flat list of (rec, score) actually included in the digest (Core + Watch,
    after the cap). Used to mark exactly the delivered papers as sent."""
    core, watch, _ = select_tiers(
        scored, tight_threshold, broad_threshold, is_top_journal, max_papers)
    out = []
    for groups in (core, watch):
        for items in groups.values():
            out.extend(items)
    return out


def _ordered_groups(groups: dict):
    keys = [k for k in GROUP_ORDER if k in groups]
    keys += [k for k in groups if k not in GROUP_ORDER]  # any unexpected area
    # Yield the human-facing label (GROUP_LABELS) rather than the stored enum key,
    # so all three renderers get the rename for free.
    return [(GROUP_LABELS.get(k, k), groups[k]) for k in keys]


def _authors_line(rec: dict) -> str:
    authors = rec.get("authors") or ""
    parts = [a.strip() for a in authors.split(",") if a.strip()]
    if not parts:
        return "(authors n/a)"
    if len(parts) <= 3:
        return ", ".join(parts)
    return f"{parts[0]} et al."


def _render_section(groups: dict) -> list[str]:
    lines: list[str] = []
    for area, items in _ordered_groups(groups):
        lines.append(f"\n## {area}  ({len(items)})")
        for rec, s in items:
            tag = " [preprint]" if rec.get("is_preprint") else ""
            title = (rec.get("title") or "(no title)").rstrip(".")
            pub_note = (f"  [now published — you saw the preprint "
                        f"{rec['prior_preprint_sent_on']}]"
                        if rec.get("prior_preprint_sent_on") else "")
            lines.append(f"\n• {title}{tag}  [{s.score}]")
            lines.append(f"  {rec.get('journal') or '?'} · {rec.get('pub_date') or '?'}{pub_note}")
            lines.append(f"  {_authors_line(rec)}")
            lines.append(f"  {s.rationale}")
            lines.append(f"  {rec.get('url') or ''}")
    return lines


def render_plaintext(scored, tight_threshold: int, run_date: str,
                     broad_threshold: int | None = None,
                     is_top_journal: Callable[[str | None], bool] = _no_journal,
                     max_papers: int = MAX_PAPERS,
                     briefing: str | None = None) -> tuple[str, int]:
    """Render the two-tier digest to plaintext. Returns (text, n_papers_in_digest)."""
    core, watch, n_omitted = select_tiers(
        scored, tight_threshold, broad_threshold, is_top_journal, max_papers)
    n_core = sum(len(v) for v in core.values())
    n_watch = sum(len(v) for v in watch.values())
    n = n_core + n_watch
    if n == 0:
        return "", 0

    lines = [
        f"Literature digest — {run_date} ({n} papers)",
        "=" * 64,
    ]
    if briefing:
        lines.append("\n" + textwrap.fill(briefing, width=76))
    if n_core:
        lines.append(f"\n# Core matches  ({n_core})")
        lines += _render_section(core)
    if n_watch:
        lines.append(
            f"\n\n# Also notable — top journals, broader scope  ({n_watch})")
        lines += _render_section(watch)
    if n_omitted:
        lines.append(f"\n(+{n_omitted} more selected, omitted by the {max_papers}-paper cap)")
    return "\n".join(lines) + "\n", n


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _html_section(groups: dict) -> list[str]:
    parts: list[str] = []
    for area, items in _ordered_groups(groups):
        parts.append(
            f'<h3 style="margin:18px 0 6px;color:#1a1a2e;'
            f'border-bottom:1px solid #ddd;padding-bottom:3px">'
            f'{_esc(area)} <span style="color:#888;font-weight:normal">({len(items)})</span></h3>')
        for rec, s in items:
            tag = (' <span style="background:#eef;color:#557;font-size:11px;'
                   'padding:1px 5px;border-radius:3px">preprint</span>'
                   if rec.get("is_preprint") else "")
            title = _esc((rec.get("title") or "(no title)").rstrip("."))
            url = rec.get("url") or ""
            title_html = (f'<a href="{_esc(url)}" style="color:#1a4fa0;'
                          f'text-decoration:none">{title}</a>' if url else title)
            pub_note = (
                f' <span style="background:#efe;color:#363;font-size:11px;'
                f'padding:1px 5px;border-radius:3px">now published — preprint seen '
                f'{_esc(rec["prior_preprint_sent_on"])}</span>'
                if rec.get("prior_preprint_sent_on") else "")
            parts.append(
                f'<div style="margin:0 0 14px">'
                f'<div style="font-size:15px;font-weight:600;line-height:1.35">'
                f'{title_html}{tag}{pub_note} '
                f'<span style="color:#a33;font-weight:600">[{_esc(s.score)}]</span></div>'
                f'<div style="color:#666;font-size:13px;margin:2px 0">'
                f'{_esc(rec.get("journal") or "?")} · {_esc(rec.get("pub_date") or "?")} · '
                f'{_esc(_authors_line(rec))}</div>'
                f'<div style="font-size:13px;color:#333">'
                f'{_esc(s.rationale)}</div>'
                f'</div>')
    return parts


def render_html(scored, tight_threshold: int, run_date: str,
                broad_threshold: int | None = None,
                is_top_journal: Callable[[str | None], bool] = _no_journal,
                max_papers: int = MAX_PAPERS,
                briefing: str | None = None,
                feedback_url: str | None = None) -> tuple[str, int]:
    """Render the two-tier digest to HTML. Returns (html, n_papers_in_digest).

    When `feedback_url` is set, a single 'rate these papers' link to the local
    feedback page (src/webfeedback.py) is shown under the header."""
    core, watch, n_omitted = select_tiers(
        scored, tight_threshold, broad_threshold, is_top_journal, max_papers)
    n_core = sum(len(v) for v in core.values())
    n_watch = sum(len(v) for v in watch.values())
    n = n_core + n_watch
    if n == 0:
        return "", 0

    p: list[str] = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:680px;margin:0 auto;color:#222">',
        f'<h1 style="font-size:20px;margin:0 0 2px">Literature digest</h1>',
        f'<div style="color:#888;font-size:13px;margin-bottom:10px">'
        f'{_esc(run_date)} · {n} paper(s)</div>',
    ]
    if briefing:
        p.append(
            f'<div style="background:#f5f7fa;border-left:3px solid #1a4fa0;'
            f'padding:10px 12px;margin:0 0 16px;font-size:14px;line-height:1.5;'
            f'color:#333">{_esc(briefing)}</div>')
    if feedback_url:
        p.append(
            f'<div style="font-size:13px;margin:0 0 14px">'
            f'<a href="{_esc(feedback_url)}" style="color:#1a4fa0">'
            f'Rate these papers →</a> '
            f'<span style="color:#999">(opens your local feedback page)</span></div>')
    if n_core:
        p.append(f'<h2 style="font-size:16px;color:#1a1a2e;margin:20px 0 4px">'
                 f'Core matches ({n_core})</h2>')
        p += _html_section(core)
    if n_watch:
        p.append(f'<h2 style="font-size:16px;color:#1a1a2e;margin:24px 0 4px">'
                 f'Also notable — top journals, broader scope ({n_watch})</h2>')
        p += _html_section(watch)
    if n_omitted:
        p.append(f'<div style="color:#999;font-size:12px;margin-top:14px">'
                 f'+{n_omitted} more selected, omitted by the {max_papers}-paper cap</div>')
    p.append("</div>")
    return "\n".join(p), n


# -- Slack (mrkdwn) ---------------------------------------------------------
_SLACK_LIMIT = 3500  # keep each posted message well under Slack's text/block caps


def _slack_esc(x) -> str:
    """Escape the three characters Slack mrkdwn treats specially in text."""
    return (str(x if x is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _slack_paper(rec, s) -> str:
    title = _slack_esc((rec.get("title") or "(no title)").rstrip("."))
    url = rec.get("url") or ""
    title_md = f"<{url}|{title}>" if url else title
    tag = " `[preprint]`" if rec.get("is_preprint") else ""
    pub = (f"  _now published — preprint seen {_slack_esc(rec['prior_preprint_sent_on'])}_"
           if rec.get("prior_preprint_sent_on") else "")
    return (f"*{title_md}*{tag}\n"
            f"_{_slack_esc(rec.get('journal') or '?')} · "
            f"{_slack_esc(rec.get('pub_date') or '?')}_{pub}\n"
            f"> {_slack_esc(s.rationale)}")


def render_slack(scored, tight_threshold: int, run_date: str,
                 broad_threshold: int | None = None,
                 is_top_journal: Callable[[str | None], bool] = _no_journal,
                 max_papers: int = MAX_PAPERS,
                 briefing: str | None = None) -> list[str]:
    """Render the digest as a list of Slack mrkdwn messages (chunked under the
    per-message limit). Empty list when there are no papers."""
    core, watch, n_omitted = select_tiers(
        scored, tight_threshold, broad_threshold, is_top_journal, max_papers)
    n_core = sum(len(v) for v in core.values())
    n_watch = sum(len(v) for v in watch.values())
    n = n_core + n_watch
    if n == 0:
        return []

    # Atomic blocks: header / tier label / area label / one paper. We never split
    # within a block, so a paper's lines always stay together.
    blocks: list[str] = [f"*Literature digest — {_slack_esc(run_date)}*  ({n} papers)"]
    if briefing:
        blocks.append(f"_{_slack_esc(briefing)}_")

    def emit(label: str, groups: dict):
        blocks.append(f"*{label}*")
        for area, items in _ordered_groups(groups):
            blocks.append(f"*{_slack_esc(area)}* ({len(items)})")
            blocks.extend(_slack_paper(rec, s) for rec, s in items)

    if n_core:
        emit(f"Core matches ({n_core})", core)
    if n_watch:
        emit(f"Also notable — top journals ({n_watch})", watch)
    if n_omitted:
        blocks.append(f"_+{n_omitted} more selected, omitted by the {max_papers}-paper cap_")

    # Greedily pack blocks into messages under the limit.
    messages: list[str] = []
    cur = ""
    for blk in blocks:
        add = ("\n\n" + blk) if cur else blk
        if cur and len(cur) + len(add) > _SLACK_LIMIT:
            messages.append(cur)
            cur = blk
        else:
            cur += add
    if cur:
        messages.append(cur)
    return messages
