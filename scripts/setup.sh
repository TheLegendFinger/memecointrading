#!/usr/bin/env bash
# One-time setup for macOS and Linux.
#
#   ./scripts/setup.sh
#
# Creates a virtual environment, installs dependencies, copies the example
# config. Safe to re-run.

set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

find_python() {
  for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
          >/dev/null 2>&1; then
        printf '%s' "$candidate"
        return 0
      fi
    fi
  done
  printf ''
}

PYTHON="$(find_python)"
if [ -z "$PYTHON" ]; then
  printf '\n  %sPython 3.9 or newer was not found.%s\n' "$RED" "$RESET" >&2
  say ""
  say "On macOS, the easiest fix is Homebrew:"
  say "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  say "    brew install python"
  say ""
  say "Or download it from https://www.python.org/downloads/"
  say ""
  exit 1
fi
info "Using $PYTHON ($("$PYTHON" --version 2>&1))"

if [ -z "$PY" ]; then
  info "Creating virtual environment (.venv)..."
  "$PYTHON" -m venv .venv || fail "Could not create the virtual environment. Try by hand:
    $PYTHON -m venv .venv"
  PY="$(resolve_venv_python)"
  [ -n "$PY" ] || fail "The virtual environment was created but has no interpreter."
fi

info "Installing dependencies (this takes a minute)..."
"$PY" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$PY" -m pip install -r requirements.txt --quiet --disable-pip-version-check \
  || fail "Dependency installation failed. Run this to see the full error:
    $PY -m pip install -r requirements.txt"

"$PY" -c 'import memebot' >/dev/null 2>&1 \
  || fail "Installed, but 'import memebot' failed. Are you running this from the repository folder?"

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  ok "Created config.yaml from the example."
fi
if [ ! -f .env ]; then
  cp .env.example .env
  ok "Created .env from the example."
fi

printf '\n'
ok "Setup complete."
printf '\n'
say "Start here            :  ./scripts/start.sh"
say "Check the market feeds:  ./scripts/doctor.sh"
say "Wallet                :  ./scripts/wallet.sh"
printf '\n'
