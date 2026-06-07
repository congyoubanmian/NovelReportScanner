#!/bin/sh
set -eu

if [ "${NOVEL_REPORT_SCANNER_REQUIRE_API_KEY:-1}" != "0" ]; then
  if [ -z "${API_KEY:-}" ] && [ -z "${API_KEY_POOL:-}" ]; then
    echo "ERROR: API_KEY or API_KEY_POOL must be set before starting the scanner container." >&2
    echo "Set NOVEL_REPORT_SCANNER_REQUIRE_API_KEY=0 only when you intentionally want to start Web without scan capability." >&2
    exit 1
  fi
fi

exec "$@"
