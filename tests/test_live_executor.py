"""Live-executor tests.

These never touch mainnet: the Jupiter client and the Solana RPC are both
faked. What they verify is the safety envelope (nothing trades unless armed)
and that a confirmed transaction is turned into correct accounting.
"""

import base64

import pytest

from memebot.config import BotConfig
from memebot.data.jupiter import JupiterQuote
from memebot.execution.live import CONFIRM_ENV, CONFIRM_VALUE, LiveExecutor
from memebot.models import Order, Side, Token, WSOL_MINT

TOKEN = Token("mint-wif", "WIF", decimals=6)


class FakeJupiter:
    def __init__(self, out_amount=1_000_000, price_impact=0.5, route=True):
        self.out_amount = out_amount
        self.price_impact = price_impact
        self.route = route
        self.swap_calls = []
        self._decimals = {WSOL_MINT: 9, TOKEN.address: 6}

    def price(self, mint):
        return 150.0 if mint == WSOL_MINT else 0.01

    def decimals(self, mint, default=9):
        return self._decimals.get(mint, default)

    def to_base_units(self, mint, amount):
        return int(round(amount * 10 ** self.decimals(mint)))

    def from_base_units(self, mint, amount):
        return amount / 10 ** self.decimals(mint)

    def quote(self, input_mint, output_mint, amount, slippage_bps, **_kw):
        if not self.route:
            return None
        return JupiterQuote(input_mint, output_mint, amount, self.out_amount,
                            int(self.out_amount * 0.98), self.price_impact,
                            slippage_bps, ["Raydium"], {"quote": True})

    def swap_transaction(self, quote, pubkey, **kwargs):
        self.swap_calls.append((quote, pubkey, kwargs))
        return {"swapTransaction": base64.b64encode(b"unsigned-tx").decode()}


class FakeRpc:
    def __init__(self, lamports=1_000_000_000, status=None, tx=None):
        self.lamports = lamports
        self.status = status if status is not None else {"confirmationStatus": "confirmed", "err": None}
        self.tx = tx
        self.sent = []

    def get_balance_lamports(self, pubkey):
        return self.lamports

    def send_raw_transaction(self, payload, max_retries=3):
        self.sent.append(payload)
        return "SIG123"

    def signature_status(self, signature):
        return self.status

    def get_transaction(self, signature):
        return self.tx


def make_executor(monkeypatch, jupiter=None, rpc=None, armed=True, wallet="Wallet111"):
    cfg = BotConfig()
    cfg.mode = "live"
    if armed:
        monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)
    else:
        monkeypatch.delenv(CONFIRM_ENV, raising=False)
    executor = LiveExecutor(cfg, jupiter=jupiter or FakeJupiter(), rpc=rpc or FakeRpc())
    # Skip real key loading; signing itself is exercised separately.
    executor._keypair = object()
    executor._pubkey = wallet
    executor._ensure_wallet = lambda: executor._keypair
    monkeypatch.setattr(executor, "_sign_and_send",
                        lambda tx_b64: executor.rpc.send_raw_transaction(tx_b64))
    return executor


def buy_order(usd=150.0, price=0.01, slippage_bps=150):
    return Order(token=TOKEN, side=Side.BUY, reference_price=price,
                 usd_amount=usd, slippage_bps=slippage_bps)


# ---- safety --------------------------------------------------------------------
def test_unarmed_executor_refuses_to_trade(monkeypatch):
    executor = make_executor(monkeypatch, armed=False)
    assert "not armed" in executor.preflight()

    fill = executor.execute(buy_order())
    assert not fill.ok
    assert "not armed" in fill.error
    assert executor.rpc.sent == []


def test_underfunded_wallet_blocks_trading(monkeypatch):
    executor = make_executor(monkeypatch, rpc=FakeRpc(lamports=1_000))
    assert "top it up" in executor.preflight()
    assert not executor.execute(buy_order()).ok


def test_armed_and_funded_executor_passes_preflight(monkeypatch):
    assert make_executor(monkeypatch).preflight() is None


def test_missing_key_is_reported_not_raised(monkeypatch):
    cfg = BotConfig()
    cfg.mode = "live"
    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    executor = LiveExecutor(cfg, jupiter=FakeJupiter(), rpc=FakeRpc())
    problem = executor.preflight()
    assert problem and ("SOLANA_PRIVATE_KEY" in problem or "solders" in problem)


def test_bad_private_key_is_reported(monkeypatch):
    cfg = BotConfig()
    cfg.mode = "live"
    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", "definitely-not-base58-!!!")
    executor = LiveExecutor(cfg, jupiter=FakeJupiter(), rpc=FakeRpc())
    problem = executor.preflight()
    assert problem and ("not a valid base58" in problem or "solders" in problem)


# ---- routing guards ------------------------------------------------------------
def test_no_route_means_no_transaction(monkeypatch):
    executor = make_executor(monkeypatch, jupiter=FakeJupiter(route=False))
    fill = executor.execute(buy_order())
    assert not fill.ok and fill.error == "no route found"
    assert executor.rpc.sent == []


def test_excessive_price_impact_is_refused(monkeypatch):
    # 9% impact against a 1.5% tolerance.
    executor = make_executor(monkeypatch, jupiter=FakeJupiter(price_impact=9.0))
    fill = executor.execute(buy_order(slippage_bps=150))
    assert not fill.ok and "price impact" in fill.error
    assert executor.rpc.sent == []


def test_unconfirmed_transaction_is_not_booked(monkeypatch):
    executor = make_executor(monkeypatch, rpc=FakeRpc(status={"err": "InstructionError"}))
    fill = executor.execute(buy_order())
    assert not fill.ok
    assert "reverted" in fill.error
    assert fill.tx_signature == "SIG123"


def test_zero_size_order_is_refused(monkeypatch):
    executor = make_executor(monkeypatch)
    fill = executor.execute(buy_order(usd=0.0))
    assert not fill.ok and "rounds to zero" in fill.error


# ---- accounting ----------------------------------------------------------------
def test_confirmed_buy_uses_on_chain_settled_amounts(monkeypatch):
    # 1 SOL in (at $150), 20,000 WIF out -> $0.0075 per token.
    tx = {
        "meta": {
            "fee": 5_000,
            "preTokenBalances": [],
            "postTokenBalances": [
                {"owner": "Wallet111", "mint": TOKEN.address,
                 "uiTokenAmount": {"uiAmount": 20_000.0}}
            ],
            "preBalances": [2_000_000_000],
            "postBalances": [1_000_000_000],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": "Wallet111"}]}},
    }
    executor = make_executor(monkeypatch, rpc=FakeRpc(tx=tx))
    fill = executor.execute(buy_order(usd=150.0, price=0.0075))

    assert fill.ok
    assert fill.token_amount == pytest.approx(20_000.0)
    # 1 SOL spent (fee added back) at $150
    assert fill.usd_amount == pytest.approx(150.0, rel=1e-3)
    assert fill.price == pytest.approx(0.0075, rel=1e-3)
    assert fill.fee_usd == pytest.approx(5_000 / 1e9 * 150.0)
    assert fill.tx_signature == "SIG123"
    assert abs(fill.slippage_bps) < 10


def test_settled_amounts_fall_back_to_the_quote(monkeypatch):
    # No parseable transaction: use the quote's expected output.
    jupiter = FakeJupiter(out_amount=20_000_000_000)  # 20,000 tokens at 6 decimals
    executor = make_executor(monkeypatch, jupiter=jupiter, rpc=FakeRpc(tx=None))
    fill = executor.execute(buy_order(usd=150.0, price=0.0075))

    assert fill.ok
    assert fill.token_amount == pytest.approx(20_000.0)
    assert fill.usd_amount == pytest.approx(150.0)
    assert fill.fee_usd == 0.0


def test_sell_prices_from_the_quote_currency_received(monkeypatch):
    jupiter = FakeJupiter(out_amount=1_000_000_000)  # 1 SOL out
    executor = make_executor(monkeypatch, jupiter=jupiter, rpc=FakeRpc(tx=None))
    order = Order(token=TOKEN, side=Side.SELL, reference_price=0.0075,
                  token_amount=20_000.0, slippage_bps=150)
    fill = executor.execute(order)

    assert fill.ok
    assert fill.token_amount == pytest.approx(20_000.0)
    assert fill.usd_amount == pytest.approx(150.0)
    assert fill.price == pytest.approx(0.0075)
    assert fill.cash_delta == pytest.approx(150.0)


def test_realised_slippage_is_signed_against_us(monkeypatch):
    jupiter = FakeJupiter(out_amount=19_000_000_000)  # fewer tokens than hoped
    executor = make_executor(monkeypatch, jupiter=jupiter, rpc=FakeRpc(tx=None))
    fill = executor.execute(buy_order(usd=150.0, price=0.0075))
    # Paid 150/19000 = 0.00789 vs a 0.0075 reference: ~5.3% adverse.
    assert fill.ok and fill.slippage_bps > 500


def test_priority_fee_settings_reach_jupiter(monkeypatch):
    jupiter = FakeJupiter()
    executor = make_executor(monkeypatch, jupiter=jupiter)
    executor.cfg.priority_fee_microlamports = 123_456
    executor.execute(buy_order())
    _quote, pubkey, kwargs = jupiter.swap_calls[-1]
    assert pubkey == "Wallet111"
    assert kwargs["priority_fee_microlamports"] == 123_456
