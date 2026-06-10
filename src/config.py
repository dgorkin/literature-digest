"""Config and path loading.

`config/` files are data, not code (spec §3): they are reloaded on every run so a
non-programmer can tune behavior without touching Python. Secrets come from the
environment / a gitignored `.env`, never from these files.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


def load_env() -> None:
    """Load the gitignored .env (if present) into the process environment."""
    load_dotenv(PROJECT_ROOT / ".env")


def load_query_config() -> dict:
    """Parse config/query.yaml fresh (no caching — config is reloaded per run)."""
    with open(CONFIG_DIR / "query.yaml") as f:
        return yaml.safe_load(f)


def get_db_path() -> Path:
    return DATA_DIR / "digest.sqlite"


def ncbi_settings() -> dict:
    """NCBI courtesy/identity params from env (spec §6).

    `tool` and `email` are the identifiers NCBI asks callers to send; `api_key`
    raises the rate limit from ~3/s to ~10/s when present.
    """
    return {
        "api_key": os.getenv("NCBI_API_KEY"),
        "tool": os.getenv("NCBI_TOOL", "literature-digest"),
        "email": os.getenv("NCBI_EMAIL"),
    }


def smtp_settings() -> dict:
    """SMTP delivery params from env (Phase 3). All from the gitignored .env.

    `to` may be a comma-separated list of recipients. With Gmail as the relay,
    `password` is an app password (not the account password) and `user`/`from`
    are the sending Gmail address.
    """
    return {
        "host": os.getenv("SMTP_HOST"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER"),
        "password": os.getenv("SMTP_PASSWORD"),
        "from": os.getenv("DIGEST_FROM") or os.getenv("SMTP_USER"),
        "to": os.getenv("DIGEST_TO"),
    }


def web_feedback_settings() -> dict:
    """Local click-to-rate web page (Phase 5). The page runs on this machine
    bound to localhost and writes ratings to the same ledger; reach it over an
    SSH tunnel. `url` is what the digest's 'Rate these papers' link points at
    (defaults to the localhost host:port); set FEEDBACK_URL only if you tunnel to
    a different local port. `days` bounds how far back the page lists sent papers.
    """
    host = os.getenv("FEEDBACK_HOST", "127.0.0.1")
    port = int(os.getenv("FEEDBACK_PORT", "8765"))
    return {
        "host": host,
        "port": port,
        "url": os.getenv("FEEDBACK_URL") or f"http://localhost:{port}/",
        "days": int(os.getenv("FEEDBACK_DAYS", "14")),
    }


def slack_webhook() -> str | None:
    """Slack Incoming Webhook URL for posting the digest (Phase 3+, optional).
    When set, the daily --send run also posts to that channel; absent -> skipped."""
    return os.getenv("SLACK_WEBHOOK_URL") or None
