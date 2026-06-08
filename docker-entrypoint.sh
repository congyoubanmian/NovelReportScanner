#!/bin/sh
set -eu

if [ "${NOVEL_REPORT_SCANNER_REQUIRE_API_KEY:-1}" != "0" ]; then
  if [ -z "${API_KEY:-}" ] && [ -z "${API_KEY_POOL:-}" ]; then
    echo "ERROR: API_KEY or API_KEY_POOL must be set before starting the scanner container." >&2
    echo "Set NOVEL_REPORT_SCANNER_REQUIRE_API_KEY=0 only when you intentionally want to start Web without scan capability." >&2
    exit 1
  fi
fi

if [ -z "${WEB_ACCESS_TOKEN:-}" ] && [ "${WEB_ALLOW_NO_AUTH:-0}" != "1" ]; then
  echo "ERROR: WEB_ACCESS_TOKEN must be set before starting the scanner container." >&2
  echo "Set WEB_ALLOW_NO_AUTH=1 only for trusted local-only deployments." >&2
  exit 1
fi

check_writable_dir() {
  dir="$1"
  mkdir -p "$dir" 2>/dev/null || true
  test_file="$dir/.write-test-$$"
  if ! ( : > "$test_file" ) 2>/dev/null; then
    echo "ERROR: $dir is not writable by the container user." >&2
    echo "Fix host permissions, for example:" >&2
    echo "  mkdir -p novels results" >&2
    echo "  chown -R ${PUID:-1000}:${PGID:-1000} novels results" >&2
    exit 1
  fi
  rm -f "$test_file"
}

check_writable_dir /app/novels
check_writable_dir /app/results

exec "$@"
