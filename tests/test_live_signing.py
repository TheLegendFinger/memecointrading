"""Signing tests that use real solders cryptography.

Skipped when the live extras are not installed, so the default suite still
runs anywhere. When they are installed - which is the case on any machine
about to trade for real - these prove the transaction that leaves the bot is
the one Jupiter built, signed by the configured wallet and nobody else.
"""

import base64

import pytest

solders = pytest.importorskip("solders", reason="live extras not installed")

from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from memebot.config import BotConfig  # noqa: E402
from memebot.execution.live import CONFIRM_ENV, CONFIRM_VALUE, LiveExecutor  # noqa: E402


def unsigned_transaction(payer: Keypair) -> str:
    """A v0 transaction shaped like the one Jupiter's /swap endpoint returns."""
    program = Pubkey.from_string("11111111111111111111111111111111")
    instruction = Instruction(
        program,
        b"\x02\x00\x00\x00" + (1000).to_bytes(8, "little"),
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(Pubkey.new_unique(), False, True)],
    )
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(message, [Signature.default()]))).decode()


class RecordingRpc:
    def __init__(self):
        self.sent = []

    def get_balance_lamports(self, pubkey):
        return 1_000_000_000

    def send_raw_transaction(self, payload, max_retries=3):
        self.sent.append(payload)
        return "SIG"


def executor_for(keypair, rpc=None):
    config = BotConfig()
    config.mode = "live"
    executor = LiveExecutor(config, jupiter=object(), rpc=rpc or RecordingRpc())
    executor._keypair = keypair
    executor._pubkey = str(keypair.pubkey())
    executor._ensure_wallet = lambda: keypair
    return executor


def test_signed_transaction_verifies():
    payer = Keypair()
    rpc = RecordingRpc()
    executor = executor_for(payer, rpc)

    executor._sign_and_send(unsigned_transaction(payer))
    signed = VersionedTransaction.from_bytes(base64.b64decode(rpc.sent[0]))

    signed.verify_with_results()  # raises if any signature is bad
    assert signed.signatures[0] != Signature.default()
    assert str(signed.message.account_keys[0]) == str(payer.pubkey())


def test_signing_does_not_alter_the_message():
    """We sign what Jupiter built - the instructions must be untouched."""
    payer = Keypair()
    rpc = RecordingRpc()
    original = unsigned_transaction(payer)
    executor_for(payer, rpc)._sign_and_send(original)

    before = VersionedTransaction.from_bytes(base64.b64decode(original))
    after = VersionedTransaction.from_bytes(base64.b64decode(rpc.sent[0]))
    assert bytes(after.message) == bytes(before.message)


def test_a_mismatched_key_refuses_to_send_anything():
    """A transaction built for another wallet must never reach the network."""
    from memebot.execution.live import LiveExecutionError

    payer = Keypair()
    stranger = Keypair()
    rpc = RecordingRpc()

    with pytest.raises(LiveExecutionError, match="is not the signer"):
        executor_for(stranger, rpc)._sign_and_send(unsigned_transaction(payer))
    assert rpc.sent == [], "nothing may be broadcast when signing fails"


def test_a_signing_mismatch_surfaces_as_a_failed_fill(monkeypatch):
    """execute() turns it into an unfilled order, not a crash."""
    from memebot.models import Order, Side, Token

    payer = Keypair()
    stranger = Keypair()
    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)
    executor = executor_for(stranger)
    monkeypatch.setattr(executor, "_execute", lambda order: executor._sign_and_send(
        unsigned_transaction(payer)))

    fill = executor.execute(Order(token=Token("mint", "X"), side=Side.BUY,
                                  reference_price=1.0, usd_amount=10.0))
    assert not fill.ok
    assert "is not the signer" in fill.error


# ---- keypair loading -----------------------------------------------------------
def test_base58_key_round_trips_through_the_environment(monkeypatch):
    from memebot.execution.live import _load_keypair

    keypair = Keypair()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", str(keypair))
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    assert str(_load_keypair().pubkey()) == str(keypair.pubkey())


def test_solana_cli_keypair_file_is_supported(tmp_path, monkeypatch):
    import json

    from memebot.execution.live import _load_keypair

    keypair = Keypair()
    path = tmp_path / "id.json"
    path.write_text(json.dumps(list(bytes(keypair))))
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SOLANA_KEYPAIR_PATH", str(path))
    assert str(_load_keypair().pubkey()) == str(keypair.pubkey())


def test_preflight_passes_with_a_real_key_and_a_funded_wallet(monkeypatch):
    keypair = Keypair()
    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", str(keypair))

    config = BotConfig()
    config.mode = "live"
    executor = LiveExecutor(config, jupiter=object(), rpc=RecordingRpc())
    assert executor.preflight() is None
    assert executor.wallet_address == str(keypair.pubkey())


# ---- wallet helpers ------------------------------------------------------------
def test_create_keypair_returns_a_usable_pair():
    from memebot.wallet import address_from_secret, create_keypair

    address, secret = create_keypair()
    assert address_from_secret(secret) == address
    assert len(address) >= 32


def test_configured_address_reads_the_environment(monkeypatch):
    from memebot.wallet import configured_address

    keypair = Keypair()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", str(keypair))
    assert configured_address() == str(keypair.pubkey())

    monkeypatch.delenv("SOLANA_PRIVATE_KEY")
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    assert configured_address() is None


def test_a_corrupt_secret_is_rejected(monkeypatch):
    from memebot.wallet import WalletError, configured_address

    monkeypatch.setenv("SOLANA_PRIVATE_KEY", "not-a-real-key")
    with pytest.raises(WalletError, match="not a valid base58"):
        configured_address()
