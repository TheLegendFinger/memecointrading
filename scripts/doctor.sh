#!/usr/bin/env bash
# Check that the market data feeds are reachable from this machine.
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_venv
# shellcheck disable=SC2046
exec "$PY" -m memebot $(config_args) doctor "$@"
