#!/bin/bash
# Fallback launcher (the "Museum Signs.app" next to this file is nicer).
# Double-click: opens the Museum Signs editor. Keep this Terminal window open while working.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "First start: setting up Python packages (needs internet once)..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || { echo "Setup failed"; read -r; exit 1; }
fi
exec .venv/bin/python museum_signs.py "$@"
