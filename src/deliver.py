"""Email delivery (spec §11.3, Phase 3).

Sends the digest as a multipart/alternative message (plaintext + HTML) over
SMTP with STARTTLS. Designed for a Gmail relay (smtp.gmail.com:587) authenticated
with an app password, delivering to any inbox — but works with any STARTTLS SMTP.

Credentials and addresses come from the environment (see config.smtp_settings);
nothing here is configured in the tracked config files. Empty-day suppression is
the caller's job: main only calls send_digest when the digest has papers.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("deliver")

REQUIRED = ("host", "port", "user", "password", "from", "to")


def _recipients(to: str) -> list[str]:
    return [a.strip() for a in (to or "").split(",") if a.strip()]


def validate(smtp: dict) -> list[str]:
    """Return a list of missing/invalid setting names ([] means OK)."""
    missing = [k for k in REQUIRED if not smtp.get(k)]
    if "to" not in missing and not _recipients(smtp["to"]):
        missing.append("to")
    return missing


def send_digest(*, subject: str, text_body: str, html_body: str, smtp: dict) -> None:
    """Send one digest email. Raises on misconfiguration or SMTP failure so the
    caller can mark the run as errored (and NOT mark papers as sent)."""
    missing = validate(smtp)
    if missing:
        raise RuntimeError(
            f"SMTP not configured — missing {', '.join(missing)} "
            f"(set them in .env: SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/"
            f"DIGEST_FROM/DIGEST_TO)")

    recipients = _recipients(smtp["to"])
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp["from"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    log.info("Sending digest to %s via %s:%s", recipients, smtp["host"], smtp["port"])
    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as server:
        server.starttls(context=context)
        server.login(smtp["user"], smtp["password"])
        server.send_message(msg, from_addr=smtp["from"], to_addrs=recipients)
    log.info("Digest sent to %d recipient(s)", len(recipients))
