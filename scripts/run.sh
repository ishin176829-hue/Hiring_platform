#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
if [ -z "${BOSS_STATE_DIR:-}" ]; then
  BOSS_STATE_DIR="$("$PROJECT_DIR/.venv/bin/python" -c 'from boss_hiring.config import default_state_dir; print(default_state_dir())')"
  export BOSS_STATE_DIR
fi
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$PROJECT_DIR/.cache/ms-playwright}"
export PYTHONUNBUFFERED=1
exec "$PROJECT_DIR/.venv/bin/boss-hiring" serve "$@"
