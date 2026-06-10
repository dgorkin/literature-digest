"""Local click-to-rate feedback page (spec §12, Phase 5).

A dependency-free web app (stdlib http.server) that lists papers from recent
digests and lets the maintainer rate each one — miss / weak / good / core — with a
single click. A click writes straight to the same SQLite ledger via
store.record_feedback; the daily run and `--feedback-report` read it back. No
inbox, no Slack, no per-paper messages.

Runs on the digest machine bound to localhost; reach it over an SSH tunnel:
    ssh -L 8765:localhost:8765 <labserver>     # then open http://localhost:8765/
Start it (once, in the background) with:
    python -m src.webfeedback                  # honors FEEDBACK_HOST/PORT/DAYS

Single-threaded server + one short-lived SQLite connection per request, so there
are no concurrency concerns for a one-user tool.
"""
from __future__ import annotations

import argparse
import html
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlparse

from src import config, store
from src.feedback import LABEL_SCORES

log = logging.getLogger(__name__)

# Rating button colors, weakest -> strongest.
_COLORS = {"miss": "#b04a4a", "weak": "#b0822a", "good": "#3a7d44", "core": "#1a4fa0"}


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _anchor(source: str, source_id: str) -> str:
    return "p-" + quote(f"{source}-{source_id}", safe="")


def _buttons(row) -> str:
    source, sid = row["source"], row["source_id"]
    current = (row["feedback"] or "").lower()
    out = []
    for label, score in LABEL_SCORES.items():
        chosen = label == current
        color = _COLORS[label]
        style = (f"display:inline-block;font-size:13px;text-decoration:none;"
                 f"padding:4px 12px;margin-right:6px;border-radius:4px;"
                 + (f"color:#fff;background:{color};font-weight:600"
                    if chosen else f"color:{color};background:#fff;"
                    f"border:1px solid {color}"))
        href = (f"/rate?source={quote(source)}&source_id={quote(sid)}"
                f"&label={label}")
        out.append(f'<a href="{href}" style="{style}">{label}'
                   f'<span style="font-size:10px;opacity:.7"> {score}</span></a>')
    mark = ('<span style="color:#3a7d44;font-size:12px;margin-left:4px">'
            '✓ saved</span>' if current else
            '<span style="color:#bbb;font-size:12px;margin-left:4px">unrated</span>')
    return "".join(out) + mark


def _paper(row) -> str:
    title = _esc((row["title"] or "(no title)").rstrip("."))
    url = row["url"] or ""
    title_html = (f'<a href="{_esc(url)}" style="color:#1a4fa0;'
                  f'text-decoration:none" target="_blank">{title}</a>'
                  if url else title)
    tag = (' <span style="background:#eef;color:#557;font-size:11px;'
           'padding:1px 5px;border-radius:3px">preprint</span>'
           if row["is_preprint"] else "")
    score = row["relevance_score"]
    score_html = (f'<span style="color:#a33;font-weight:600">[{_esc(score)}]</span>'
                  if score is not None else "")
    return (
        f'<div id="{_anchor(row["source"], row["source_id"])}" '
        f'style="margin:0 0 20px;padding-bottom:14px;border-bottom:1px solid #eee">'
        f'<div style="font-size:15px;font-weight:600;line-height:1.35">'
        f'{title_html}{tag} {score_html}</div>'
        f'<div style="color:#666;font-size:13px;margin:2px 0">'
        f'{_esc(row["journal"] or "?")} · {_esc(row["pub_date"] or "?")} · '
        f'{_esc(row["matched_area"] or "?")} · sent {_esc(row["sent_on"])}</div>'
        f'<div style="font-size:13px;color:#333;margin-bottom:8px">'
        f'{_esc(row["relevance_rationale"] or "")}</div>'
        f'<div>{_buttons(row)}</div>'
        f'</div>')


def render_page(rows, days: int) -> str:
    n_rated = sum(1 for r in rows if r["feedback"])
    body = "".join(_paper(r) for r in rows) or (
        '<p style="color:#888">No papers sent in the last '
        f'{days} days yet.</p>')
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Rate papers</title></head>"
        "<body style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,"
        "sans-serif;max-width:720px;margin:0 auto;padding:18px;color:#222\">"
        "<h1 style='font-size:20px;margin:0 0 2px'>Rate papers</h1>"
        f"<div style='color:#888;font-size:13px;margin-bottom:18px'>"
        f"last {days} days · {len(rows)} paper(s) · {n_rated} rated · "
        f"click a rating; it saves instantly</div>"
        f"{body}</body></html>")


class FeedbackHandler(BaseHTTPRequestHandler):
    db_path = None   # set by serve()
    days = 14

    def _send(self, code: int, body: str = "", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body:
            data = body.encode("utf-8")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802 — http.server API
        parsed = urlparse(self.path)
        if parsed.path == "/rate":
            return self._handle_rate(parse_qs(parsed.query))
        if parsed.path in ("/", "/index.html"):
            conn = store.connect(self.db_path)
            rows = store.sent_for_rating(conn, self.days)
            conn.close()
            return self._send(200, render_page(rows, self.days))
        return self._send(404, "<p>Not found</p>")

    def _handle_rate(self, q: dict):
        source = (q.get("source") or [""])[0]
        source_id = (q.get("source_id") or [""])[0]
        label = (q.get("label") or [""])[0].lower()
        if not source or not source_id or label not in LABEL_SCORES:
            return self._send(400, "<p>Bad rating request</p>")
        conn = store.connect(self.db_path)
        from datetime import date
        n = store.record_feedback(conn, source, source_id, label,
                                  date.today().isoformat())
        conn.close()
        if not n:
            log.warning("rate: no such paper %s:%s", source, source_id)
        # Redirect back to the list, scrolled to the paper just rated.
        loc = "/#" + _anchor(source, source_id)
        return self._send(303, headers={"Location": loc})

    def log_message(self, fmt, *args):  # quieter than the default stderr spam
        log.info("web %s", fmt % args)


def serve(host: str, port: int, db_path, days: int) -> None:
    FeedbackHandler.db_path = db_path
    FeedbackHandler.days = days
    httpd = HTTPServer((host, port), FeedbackHandler)
    log.info("feedback page on http://%s:%d/ (db=%s, last %d days)",
             host, port, db_path, days)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        httpd.server_close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.load_env()
    cfg = config.web_feedback_settings()
    ap = argparse.ArgumentParser(description="Local click-to-rate feedback page")
    ap.add_argument("--host", default=cfg["host"])
    ap.add_argument("--port", type=int, default=cfg["port"])
    ap.add_argument("--days", type=int, default=cfg["days"])
    args = ap.parse_args()
    serve(args.host, args.port, config.get_db_path(), args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
