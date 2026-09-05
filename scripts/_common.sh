#!/usr/bin/env bash
# Shared helpers for the memebot shell scripts (macOS and Linux).
#
# Kept compatible with the bash 3.2 that ships with macOS: no associative
# arrays, no mapfile, no ${var^^}.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[38;5;203m'; GREEN=$'\033[38;5;35m'
  YELLOW=$'\033[38;5;214m'; CYAN=$'\033[38;5;39m'; RESET=$'\033[0m'
fi

say()  { printf '  %s\n' "$*"; }
info() { printf '  %s%s%s\n' "$CYAN" "$*" "$RESET"; }
ok()   { printf '  %s%s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '  %s%s%s\n' "$YELLOW" "$*" "$RESET"; }
rule() { printf '  %s\n' "------------------------------------------------------------------"; }

fail() {
  printf '\n  %s%s%s\n\n' "$RED" "$*" "$RESET" >&2
  exit 1
}

# Where the virtual environment's interpreter lives. macOS and Linux use
# bin/python; the Windows layout is checked too so one repo serves both.
resolve_venv_python() {
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    printf '%s' "$REPO_ROOT/.venv/bin/python"
  elif [ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]; then
    printf '%s' "$REPO_ROOT/.venv/Scripts/python.exe"
  else
    printf ''
  fi
}

PY="$(resolve_venv_python)"

require_venv() {
  if [ -z "$PY" ]; then
    fail "No virtual environment found. Run this first:
    ./scripts/setup.sh"
  fi
}

# Is a Python package importable? Prints nothing either way, so a missing
# package does not dump a traceback into the output.
has_module() {
  "$PY" -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec(sys.argv[1]) else 1)' "$1" \
    >/dev/null 2>&1
}

install_live_extras() {
  if has_module solders; then
    return 0
  fi
  info "Installing the Solana packages (solders, base58, mnemonic)..."
  "$PY" -m pip install -r requirements-live.txt --quiet --disable-pip-version-check || return 1
  has_module solders
}

confirm() {
  printf '  %s [y/N] ' "$1"
  read -r reply
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

config_args() {
  if [ -f "$REPO_ROOT/config.yaml" ]; then
    printf '%s' "--config config.yaml"
  fi
}
