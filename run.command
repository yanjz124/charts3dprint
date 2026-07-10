#!/bin/bash
# ============================================================
#  charts3dprint - one-click launcher (macOS / Linux)
#  macOS: double-click in Finder. Linux: run ./run.command
#  (first: chmod +x run.command)
# ============================================================
cd "$(dirname "$0")" || exit 1

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo
  echo "  Python 3.9+ is required but was not found."
  echo "  Install it from https://www.python.org/downloads/ and run this again."
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Installing dependencies (first run only, ~1-2 min)..."
"$PY" -m pip install -r requirements.txt --quiet --disable-pip-version-check

echo
echo "Starting charts3dprint - your browser will open at http://127.0.0.1:5000"
echo "Keep this window open while using the app. Close it (Ctrl+C) to quit."
echo
"$PY" -m charts3dprint --gui
