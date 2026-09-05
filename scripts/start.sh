#!/usr/bin/env bash
# The front door. Opens the menu - everything is a number from there.
#
#   ./scripts/start.sh

set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

if [ -z "$PY" ]; then
  printf '\n'
  warn "memebot is not set up on this machine yet."
  say "Setup creates a private Python environment and installs what it needs."
  printf '\n'
  if ! confirm "Run setup now?"; then
    fail "Nothing to run yet. When you are ready:
    ./scripts/setup.sh"
  fi
  "$(dirname "${BASH_SOURCE[0]}")/setup.sh" || exit $?
  PY="$(resolve_venv_python)"
  [ -n "$PY" ] || fail "Setup did not finish. See the messages above."
fi

exec "$PY" -m memebot menu
