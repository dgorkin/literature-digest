"""Slack delivery via an Incoming Webhook (Phase 3+, optional).

A webhook posts to a single channel with no bot or OAuth scopes — the URL itself
is the credential, so it lives in the environment (config.slack_webhook), never in
the tracked config files. The digest is posted as one or more mrkdwn messages
(digest.render_slack chunks long digests to stay under Slack's limits).

Slack is an ADDITIONAL channel: email remains the system of record for sent-status
(see main). A Slack failure is surfaced in the run log/`runs.error` but does not by
itself block marking papers sent when email already delivered.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("slack")


def post_digest(messages: list[str], webhook_url: str, timeout: int = 20) -> None:
    """POST each mrkdwn message to the Incoming Webhook in order. Raises on the
    first failure (non-200 or a non-"ok" body) so the caller can record it."""
    if not webhook_url:
        raise RuntimeError("Slack webhook URL is empty")
    n = len(messages)
    for i, msg in enumerate(messages, 1):
        resp = requests.post(
            webhook_url, json={"text": msg, "mrkdwn": True}, timeout=timeout)
        if resp.status_code != 200 or resp.text.strip() != "ok":
            raise RuntimeError(
                f"Slack webhook failed on message {i}/{n}: "
                f"HTTP {resp.status_code} {resp.text[:200]!r}")
    log.info("Posted digest to Slack (%d message(s))", n)
