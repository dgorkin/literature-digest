#!/usr/bin/env bash
# Daily cron wrapper for the literature digest (Phase 3).
#
# Activates the conda env, runs the full pipeline with email delivery, and
# appends a dated log. Cron has a minimal environment, so everything here is
# absolute / self-contained. Install with crontab.example.
#
#   crontab -e   # then paste the line from crontab.example
#
set -euo pipefail

# Resolve the project dir from this script's own location, so the wrapper is
# portable and cron can invoke it by absolute path from any working directory.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# conda may not be on cron's minimal PATH; override CONDA here if yours differs.
CONDA="${CONDA:-/opt/anaconda3/bin/conda}"
ENV_NAME="lit-digest"
LOG_DIR="${PROJECT_DIR}/logs"

mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/digest_$(date +%Y-%m-%d).log"

cd "${PROJECT_DIR}"

# `conda run` works without an interactive shell; `python -u` keeps stdout
# unbuffered so the log lines interleave in real time. --send delivers the email
# and marks papers sent; empty days are suppressed inside main.
{
    echo "===== run $(date -Is) ====="
    "${CONDA}" run --no-capture-output -n "${ENV_NAME}" python -u -m src.main --send
    echo "===== exit $? ($(date -Is)) ====="
} >> "${LOG_FILE}" 2>&1
