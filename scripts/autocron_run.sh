#!/bin/bash
set -euo pipefail
# AutoCron Run — Entry point for CoPaw cron integration.
#
# This script is invoked by CoPaw's cron system to run an AutoCron task.
# It activates the Python environment and calls autocron with the given task.
#
# Usage:
#   autocron_run.sh <task_file.md> [--dry-run]
#
# Environment:
#   AUTOCRON_WORKER_URL   — Worker LLM endpoint (default: http://localhost:11434)
#   AUTOCRON_WORKER_MODEL — Worker model name (default: qwen3:27b)
#   AUTOCRON_MANAGER_URL  — Manager LLM endpoint
#   AUTOCRON_MANAGER_MODEL — Manager model name (default: claude-sonnet-4-20250514)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Run autocron
exec python -m autocron "$@"
