#!/usr/bin/env bash
# The bot's wallet: address, balance, seed phrase, withdrawals.
#
#   ./scripts/wallet.sh                     show it
#   ./scripts/wallet.sh --new --save        create one
#   ./scripts/wallet.sh --phrase            show the seed phrase
#   ./scripts/wallet.sh --import            restore from a phrase
#   ./scripts/wallet.sh --withdraw          send SOL out

set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
require_venv
install_live_extras || fail "Could not install the Solana packages. Try by hand:
    $PY -m pip install -r requirements-live.txt"
# shellcheck disable=SC2046
exec "$PY" -m memebot $(config_args) wallet "$@"
