"""Decimals, and why guessing them broke every sell.

Two unrelated coins, every route, every size, the same Jupiter program error.
That is not a coin problem, it is ours - and it was this: the token metadata
lookup was failing, `decimals()` silently returned its default of 9, and most
memecoins have 6. Decimals are an exponent, so the bot asked to sell a
thousand times what the wallet held. No slippage tolerance, no smaller size
and no alternative route fixes that.

Buys were untouched, because a buy is denominated in the quote currency -
wrapped SOL, which really does have 9. That asymmetry is the fingerprint.
"""

import pytest

from memebot.data.jupiter import JupiterClient
from memebot.execution.live import LiveExecutionError, LiveExecutor
from memebot.http import HttpError
from memebot.models import WSOL_MINT

MINT = "So1CatMint1111111111111111111111111111111"


class FakeHttp:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def get(self, path, params=None, **kw):
        if self.error:
            raise self.error
        return self.response

    def post(self, *a, **kw):  # pragma: no cover - unused
        raise AssertionError("not expected")


# ---- the client no longer pretends ----------------------------------------------
def test_a_failed_metadata_lookup_admits_it_does_not_know():
    client = JupiterClient(http=FakeHttp(error=HttpError("410 Gone", 410)))
    assert client.lookup_decimals(MINT) is None


def test_metadata_without_decimals_admits_it_too():
    client = JupiterClient(http=FakeHttp(response={"symbol": "SOLCAT"}))
    assert client.lookup_decimals(MINT) is None


def test_a_real_answer_is_cached():
    http = FakeHttp(response={"decimals": 6})
    client = JupiterClient(http=http)
    assert client.lookup_decimals(MINT) == 6
    http.error = HttpError("410 Gone", 410)
    assert client.lookup_decimals(MINT) == 6, "not looked up twice"


def test_the_known_mints_never_need_a_lookup():
    client = JupiterClient(http=FakeHttp(error=HttpError("410 Gone", 410)))
    assert client.lookup_decimals(WSOL_MINT) == 9


# ---- the executor reads the chain instead ----------------------------------------
def make_executor(config, jupiter, rpc):
    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = config.execution
    executor.config = config
    executor.jupiter = jupiter
    executor.rpc = rpc
    executor._pubkey = "Wallet111"
    executor._ensure_wallet = lambda: None
    return executor


class Jupiter:
    def __init__(self, known=None):
        self.known = dict(known or {})

    def lookup_decimals(self, mint):
        return self.known.get(mint)

    def set_decimals(self, mint, decimals):
        self.known[mint] = int(decimals)


class Rpc:
    def __init__(self, decimals=6, balance=1_000.0, mint_error=None):
        self.decimals = decimals
        self.balance = balance
        self.mint_error = mint_error
        self.mint_calls = 0

    def get_mint_account(self, mint):
        self.mint_calls += 1
        if self.mint_error:
            raise self.mint_error
        return {"mintAuthority": None, "freezeAuthority": None, "decimals": self.decimals}

    def get_token_balance(self, owner, mint):
        return self.balance


def test_decimals_come_from_the_mint_account_when_metadata_is_down(config):
    """The mint account is the authority, and the safety check already reads it."""
    rpc = Rpc(decimals=6)
    executor = make_executor(config, Jupiter(), rpc)

    assert executor.token_decimals(MINT) == 6


def test_the_chain_answer_is_cached_back_into_the_client(config):
    jupiter = Jupiter()
    rpc = Rpc(decimals=6)
    executor = make_executor(config, jupiter, rpc)

    executor.token_decimals(MINT)
    executor.token_decimals(MINT)

    assert rpc.mint_calls == 1
    assert jupiter.known[MINT] == 6


def test_an_unknowable_token_is_refused_rather_than_guessed(config):
    """Being wrong here is a factor of a thousand, so there is no safe default."""
    executor = make_executor(config, Jupiter(), Rpc(mint_error=HttpError("down", 500)))

    with pytest.raises(LiveExecutionError, match="decimals"):
        executor.token_decimals(MINT)


# ---- and never asks for more than the wallet holds --------------------------------
def test_a_sell_is_capped_at_the_real_balance(config):
    """Bookkeeping drifts; asking for more than you have fails the whole swap."""
    executor = make_executor(config, Jupiter(), Rpc(balance=900.0))
    assert executor.sellable_amount(MINT, 1_000.0) == 900.0


def test_a_sell_below_the_balance_is_left_alone(config):
    executor = make_executor(config, Jupiter(), Rpc(balance=900.0))
    assert executor.sellable_amount(MINT, 100.0) == 100.0


def test_an_empty_wallet_says_so(config):
    executor = make_executor(config, Jupiter(), Rpc(balance=0.0))
    with pytest.raises(LiveExecutionError, match="nothing to sell"):
        executor.sellable_amount(MINT, 100.0)


def test_an_unreadable_balance_does_not_block_the_exit(config):
    """A read failing must never be the reason a position cannot be closed."""
    class Broken(Rpc):
        def get_token_balance(self, owner, mint):
            raise HttpError("node is down", 500)

    executor = make_executor(config, Jupiter(), Broken())
    assert executor.sellable_amount(MINT, 100.0) == 100.0


# ---- end to end, the exact failure -----------------------------------------------
def test_a_six_decimal_coin_is_sold_in_six_decimal_units(config):
    """The regression: 1234.5678 of a 6-decimal coin is 1,234,567,800 base
    units. At the old default of 9 it asked for 1,234,567,800,000 - a thousand
    times the balance - and every route refused it."""
    from memebot.models import Order, Side, Token

    asked = {}

    class Quoting(Jupiter):
        def quote(self, in_mint, out_mint, amount, slippage_bps, **kw):
            asked["amount"] = amount
            return None

        def to_base_units(self, mint, amount):  # pragma: no cover - buys only
            return int(round(amount * 1e9))

    executor = make_executor(config, Quoting(), Rpc(decimals=6, balance=5_000.0))
    executor._quote_price_usd = lambda: 150.0
    order = Order(token=Token(address=MINT, symbol="SOLCAT"), side=Side.SELL,
                  reference_price=0.01, token_amount=1234.5678)

    executor._execute(order)

    assert asked["amount"] == 1_234_567_800


def test_the_amount_is_truncated_not_rounded_up(config):
    """One base unit over the balance fails the whole swap."""
    from memebot.models import Order, Side, Token

    asked = {}

    class Quoting(Jupiter):
        def quote(self, in_mint, out_mint, amount, slippage_bps, **kw):
            asked["amount"] = amount
            return None

    executor = make_executor(config, Quoting(), Rpc(decimals=6, balance=1.9999999))
    executor._quote_price_usd = lambda: 150.0
    order = Order(token=Token(address=MINT, symbol="X"), side=Side.SELL,
                  reference_price=1.0, token_amount=1.9999999)

    executor._execute(order)

    assert asked["amount"] == 1_999_999, "rounding up would ask for a token we lack"
