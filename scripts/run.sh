#!/usr/bin/env bash
# Start trading. REAL money - read LIVE_TRADING.md before using this.
#
#   ./scripts/run.sh                trade for real

set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_venv

install_live_extras || fail "Could not install the Solana packages. Try by hand:
    $PY -m pip install -r requirements-live.txt"

# shellcheck disable=SC2046
"$PY" -m memebot $(config_args) wallet || fail "The wallet is not ready to trade - see above."

printf '\n'
warn "You are about to trade REAL money."
say "This bot buys brand-new memecoins. Many go to zero. It can lose"
say "everything in the wallet, and it keeps trading until you stop it."
printf '\n'
printf '  Type LIVE to start trading for real: '
read -r reply
[ "$reply" = "LIVE" ] || fail "Not started."

# This process only: it disappears when the shell exits, so nothing else can
# trade for real without going through here.
export LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK
set -- "$@" -y

printf '\n'
# shellcheck disable=SC2046
exec "$PY" -m memebot $(config_args) run "$@"
