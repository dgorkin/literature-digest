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


def slack_webhook() -> str | None:
    """Slack Incoming Webhook URL for posting the digest (Phase 3+, optional).
    When set, the daily --send run also posts to that channel; absent -> skipped."""
    return os.getenv("SLACK_WEBHOOK_URL") or None
