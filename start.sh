#!/usr/bin/env bash
set -euo pipefail
# start.sh — activate venv (if present) and run rpi.py
# Place this file in the project root and make it executable: `chmod +x start.sh`

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

if [ ! -f "rpi.py" ]; then
  echo "No rpi.py found in the project root." >&2
  exit 1
fi

exec python3 rpi.py
