"""Wallet tests: seed phrases, derivation, and moving funds out.

The derivation tests matter more than most. If the path or the SLIP-0010
implementation were wrong, the seed phrase shown to the user would not restore
the wallet in Phantom - which is indistinguishable from losing the funds.
"""

import pytest

solders = pytest.importorskip("solders", reason="live extras not installed")
pytest.importorskip("mnemonic", reason="live extras not installed")

from memebot.wallet import (  # noqa: E402
    DEFAULT_DERIVATION_PATH, ENV_KEY, ENV_MNEMONIC_KEY, LAMPORTS_PER_SOL, WalletError,
    address_from_mnemonic, append_to_env, configured_address, configured_mnemonic,
    create_keypair, create_wallet_with_phrase, derive_ed25519_key, generate_mnemonic,
    is_valid_address, keypair_from_mnemonic, seed_from_mnemonic, validate_mnemonic,
    withdraw_sol,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# ---- SLIP-0010, against the specification's own test vectors -------------------
def test_slip10_matches_the_published_test_vectors():
    """Vector 1 from SLIP-0010 for ed25519. If this drifts, phrases stop working."""
    seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")

    assert derive_ed25519_key(seed, "m").hex() == (
        "2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7"
    )
    assert derive_ed25519_key(seed, "m/0'").hex() == (
        "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3"
    )


def test_the_derivation_path_is_the_one_wallets_expect():
    """Phantom, Solflare and the Solana CLI all use this for the first account."""
    assert DEFAULT_DERIVATION_PATH == "m/44'/501'/0'/0'"


def test_derivation_is_deterministic():
    seed = seed_from_mnemonic("legal winner thank year wave sausage worth useful legal "
                              "winner thank yellow")
    assert derive_ed25519_key(seed) == derive_ed25519_key(seed)


def test_a_passphrase_changes_the_wallet():
    phrase = generate_mnemonic()
    assert seed_from_mnemonic(phrase) != seed_from_mnemonic(phrase, passphrase="extra")


# ---- phrases -------------------------------------------------------------------
@pytest.mark.parametrize("words", [12, 24])
def test_generated_phrases_are_the_right_length_and_valid(words):
    phrase = generate_mnemonic(words)
    assert len(phrase.split()) == words
    assert validate_mnemonic(phrase)


def test_only_12_or_24_words_are_offered():
    with pytest.raises(WalletError):
        generate_mnemonic(15)


def test_a_new_wallet_round_trips_through_its_phrase():
    address, secret, phrase = create_wallet_with_phrase()
    assert address_from_mnemonic(phrase) == address
    assert str(keypair_from_mnemonic(phrase)) == secret


def test_a_tampered_phrase_is_rejected():
    """BIP-39 has a checksum; a mistyped word must not silently make a new wallet."""
    words = generate_mnemonic().split()
    words[-1] = "zoo" if words[-1] != "zoo" else "abandon"
    broken = " ".join(words)
    if validate_mnemonic(broken):
        pytest.skip("the substitution happened to be a valid checksum")
    with pytest.raises(WalletError, match="not valid"):
        keypair_from_mnemonic(broken)


def test_junk_is_not_a_phrase():
    assert not validate_mnemonic("not actually a seed phrase at all")
    assert not validate_mnemonic("")


def test_whitespace_does_not_change_the_wallet():
    _address, _secret, phrase = create_wallet_with_phrase()
    messy = "  " + "   ".join(phrase.split()) + "\n"
    assert address_from_mnemonic(messy) == address_from_mnemonic(phrase)


def test_a_raw_wallet_has_no_phrase_but_still_works():
    address, secret = create_keypair()
    from memebot.wallet import address_from_secret

    assert address_from_secret(secret) == address


# ---- the environment -----------------------------------------------------------
def test_a_phrase_alone_configures_the_wallet(monkeypatch):
    _address, _secret, phrase = create_wallet_with_phrase()
    monkeypatch.delenv(ENV_KEY, raising=False)
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.setenv(ENV_MNEMONIC_KEY, phrase)

    assert configured_address() == address_from_mnemonic(phrase)
    assert configured_mnemonic() == phrase


def test_the_live_executor_can_trade_from_a_phrase(monkeypatch):
    from memebot.execution.live import _load_keypair

    _address, _secret, phrase = create_wallet_with_phrase()
    monkeypatch.delenv(ENV_KEY, raising=False)
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.setenv(ENV_MNEMONIC_KEY, phrase)

    assert str(_load_keypair().pubkey()) == address_from_mnemonic(phrase)


def test_a_broken_phrase_in_the_environment_is_reported_clearly(monkeypatch):
    from memebot.execution.live import LiveExecutionError, _load_keypair

    monkeypatch.delenv(ENV_KEY, raising=False)
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.setenv(ENV_MNEMONIC_KEY, "these are not real bip39 words at all")

    with pytest.raises(LiveExecutionError, match="SOLANA_MNEMONIC"):
        _load_keypair()


def test_saving_never_overwrites_an_existing_wallet(tmp_path):
    env = tmp_path / ".env"
    append_to_env({ENV_MNEMONIC_KEY: "first phrase"}, str(env))
    with pytest.raises(WalletError, match="already set"):
        append_to_env({ENV_MNEMONIC_KEY: "second phrase"}, str(env))
    assert "first phrase" in env.read_text()


# ---- withdrawals ---------------------------------------------------------------
class FakeRpc:
    def __init__(self, lamports=250_000_000, status=None, blockhash="11111111111111111111111111111111"):
        self.lamports = lamports
        self.status = status if status is not None else {"confirmationStatus": "confirmed", "err": None}
        self.blockhash = blockhash
        self.sent = []

    def get_balance_lamports(self, pubkey):
        return self.lamports

    def get_latest_blockhash(self):
        return {"blockhash": self.blockhash} if self.blockhash else {}

    def send_raw_transaction(self, payload, max_retries=3):
        self.sent.append(payload)
        return "SIGNATURE"

    def signature_status(self, signature):
        return self.status


def wallet():
    from solders.keypair import Keypair

    return Keypair()


def test_a_withdrawal_sends_a_signed_transfer():
    import base64

    from solders.transaction import VersionedTransaction

    rpc, keypair = FakeRpc(), wallet()
    result = withdraw_sol(rpc, keypair, USDC, 100_000_000)

    assert result["confirmed"] and result["sol"] == pytest.approx(0.1)
    signed = VersionedTransaction.from_bytes(base64.b64decode(rpc.sent[0]))
    signed.verify_with_results()          # raises if the signature is bad
    data = bytes(signed.message.instructions[0].data)
    assert int.from_bytes(data[4:12], "little") == 100_000_000


def test_withdrawing_everything_leaves_the_fee_behind():
    rpc, keypair = FakeRpc(lamports=250_000_000), wallet()
    result = withdraw_sol(rpc, keypair, USDC)
    assert result["lamports"] == 250_000_000 - 10_000


def test_sending_to_this_wallet_is_refused():
    keypair = wallet()
    with pytest.raises(WalletError, match="own address"):
        withdraw_sol(FakeRpc(), keypair, str(keypair.pubkey()), 1000)


def test_sending_more_than_the_balance_is_refused():
    with pytest.raises(WalletError, match="Not enough SOL"):
        withdraw_sol(FakeRpc(lamports=1_000_000), wallet(), USDC, 900_000_000)


def test_an_empty_wallet_is_refused_before_any_transaction():
    rpc = FakeRpc(lamports=5_000)
    with pytest.raises(WalletError, match="Nothing to send"):
        withdraw_sol(rpc, wallet(), USDC)
    assert rpc.sent == [], "nothing may be broadcast"


def test_a_bad_destination_is_refused_before_any_transaction():
    rpc = FakeRpc()
    with pytest.raises(WalletError, match="not a valid Solana address"):
        withdraw_sol(rpc, wallet(), "definitely-not-an-address", 1000)
    assert rpc.sent == []


def test_a_missing_blockhash_is_reported():
    with pytest.raises(WalletError, match="blockhash"):
        withdraw_sol(FakeRpc(blockhash=""), wallet(), USDC, 1000)


def test_a_reverted_withdrawal_is_reported_not_claimed_as_sent():
    rpc = FakeRpc(status={"err": "InsufficientFundsForRent"})
    result = withdraw_sol(rpc, wallet(), USDC, 1000)
    assert not result["confirmed"]
    assert "reverted" in result["error"]
    assert result["signature"] == "SIGNATURE"


def test_address_validation():
    assert is_valid_address(USDC)
    assert not is_valid_address("nope")
    assert not is_valid_address("")
