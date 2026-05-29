#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

BOOTSTRAP_SCRIPT="bootstrap_venv.py"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
EXIT_CODE=0

if [ -x "$VENV_PYTHON" ]; then
    "$VENV_PYTHON" "$BOOTSTRAP_SCRIPT"
    EXIT_CODE=$?
elif command -v python3 >/dev/null 2>&1; then
    python3 "$BOOTSTRAP_SCRIPT"
    EXIT_CODE=$?
elif command -v python >/dev/null 2>&1; then
    python "$BOOTSTRAP_SCRIPT"
    EXIT_CODE=$?
else
    echo "[ERROR] Python 3.10+ was not found."
    echo "[ERROR] Install Python 3.10 or newer and add it to PATH."
    EXIT_CODE=1
fi

echo
if [ -t 0 ]; then
    printf 'Task finished. Press any key to exit...'
    read -r -n 1 -s
    echo
else
    echo "Task finished."
fi

exit "$EXIT_CODE"
