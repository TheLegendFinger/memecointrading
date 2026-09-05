"""Wallet helpers for live trading.

Creating a burner wallet, showing its address so you can fund it, and reading
its balances. Kept apart from the executor because these run before any trading
is armed - and because none of it should ever print a secret key by accident.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENV_KEY = "SOLANA_PRIVATE_KEY"
ENV_PATH_KEY = "SOLANA_KEYPAIR_PATH"
ENV_MNEMONIC_KEY = "SOLANA_MNEMONIC"

# Phantom, Solflare and the Solana CLI all use this path for the first account,
# so a phrase created here restores in any of them.
DEFAULT_DERIVATION_PATH = "m/44'/501'/0'/0'"

LAMPORTS_PER_SOL = 1_000_000_000
# A simple transfer costs 5000 lamports; leave room for a priority bump.
TRANSFER_FEE_LAMPORTS = 10_000


class WalletError(RuntimeError):
    pass


def install_hint() -> str:
    """How to install the extras, in the form this platform actually uses."""
    import sys

    if os.name == "nt":
        return "  .\\.venv\\Scripts\\python.exe -m pip install -r requirements-live.txt"
    return f"  {sys.executable} -m pip install -r requirements-live.txt"


def _keypair_module():
    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise WalletError(
            "Creating a wallet needs the 'solders' package:\n" + install_hint()
        ) from exc
    return Keypair


def create_keypair() -> Tuple[str, str]:
    """Generate a raw wallet with no seed phrase. Returns (address, base58 secret)."""
    Keypair = _keypair_module()
    keypair = Keypair()
    return str(keypair.pubkey()), str(keypair)


# --------------------------------------------------------------------------------
# seed phrases (BIP-39) and Solana key derivation (SLIP-0010, ed25519)
# --------------------------------------------------------------------------------
def _bip39():
    try:
        from mnemonic import Mnemonic  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise WalletError(
            "Seed phrases need the 'mnemonic' package:\n" + install_hint()
        ) from exc
    return Mnemonic("english")


def generate_mnemonic(words: int = 12) -> str:
    """A fresh BIP-39 phrase. 12 words (128 bits) or 24 (256 bits)."""
    if words not in (12, 24):
        raise WalletError("A seed phrase is either 12 or 24 words")
    return _bip39().generate(strength=128 if words == 12 else 256)


def validate_mnemonic(phrase: str) -> bool:
    """Checksum-check a phrase before anyone relies on it."""
    try:
        return _bip39().check(" ".join(phrase.split()))
    except WalletError:
        raise
    except Exception:  # noqa: BLE001 - malformed input is simply invalid
        return False


def seed_from_mnemonic(phrase: str, passphrase: str = "") -> bytes:
    """BIP-39 phrase -> 64-byte seed."""
    return _bip39().to_seed(" ".join(phrase.split()), passphrase=passphrase)


def _slip10_master(seed: bytes) -> Tuple[bytes, bytes]:
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def _slip10_child(key: bytes, chain_code: bytes, index: int) -> Tuple[bytes, bytes]:
    # ed25519 supports hardened derivation only, so the index is always hardened.
    index |= 0x80000000
    data = b"\x00" + key + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def derive_ed25519_key(seed: bytes, path: str = DEFAULT_DERIVATION_PATH) -> bytes:
    """SLIP-0010 ed25519 derivation. Verified against the specification's own
    test vectors - an error here would produce a phrase that does not restore
    the wallet anywhere else, which is indistinguishable from losing the funds.
    """
    key, chain_code = _slip10_master(seed)
    for element in path.split("/"):
        if element in ("m", ""):
            continue
        key, chain_code = _slip10_child(key, chain_code, int(element.rstrip("\'")))
    return key


def keypair_from_mnemonic(phrase: str, path: str = DEFAULT_DERIVATION_PATH,
                          passphrase: str = ""):
    """The wallet a phrase corresponds to, on the standard Solana path."""
    if not validate_mnemonic(phrase):
        raise WalletError(
            "That seed phrase is not valid - check the words and their order. "
            "(Each word must be from the BIP-39 list, and the phrase has a checksum.)"
        )
    Keypair = _keypair_module()
    derived = derive_ed25519_key(seed_from_mnemonic(phrase, passphrase), path)
    return Keypair.from_seed(derived)


def create_wallet_with_phrase(words: int = 12) -> Tuple[str, str, str]:
    """A new wallet backed by a seed phrase.

    Returns (address, base58 secret, phrase). The phrase is the real backup:
    it restores this wallet in Phantom, Solflare or the Solana CLI.
    """
    phrase = generate_mnemonic(words)
    keypair = keypair_from_mnemonic(phrase)
    return str(keypair.pubkey()), str(keypair), phrase


def address_from_mnemonic(phrase: str) -> str:
    return str(keypair_from_mnemonic(phrase).pubkey())


def address_from_secret(secret: str) -> str:
    """Derive the public address from a base58 secret, validating it."""
    Keypair = _keypair_module()
    try:
        return str(Keypair.from_base58_string(secret.strip()).pubkey())
    except Exception as exc:
        raise WalletError(f"That is not a valid base58 secret key: {exc}") from exc


def configured_mnemonic() -> str:
    return os.environ.get(ENV_MNEMONIC_KEY, "").strip()


def configured_address() -> Optional[str]:
    """The address of whichever key the environment currently points at."""
    secret = os.environ.get(ENV_KEY, "").strip()
    if secret:
        return address_from_secret(secret)

    phrase = configured_mnemonic()
    if phrase:
        return address_from_mnemonic(phrase)

    path = os.environ.get(ENV_PATH_KEY, "").strip()
    if path:
        import json

        Keypair = _keypair_module()
        try:
            with open(os.path.expanduser(path), "r") as handle:
                data = json.load(handle)
            return str(Keypair.from_bytes(bytes(data)).pubkey())
        except Exception as exc:
            raise WalletError(f"Could not read the keypair at {path}: {exc}") from exc
    return None


# --------------------------------------------------------------------------------
# moving funds out
# --------------------------------------------------------------------------------
def is_valid_address(address: str) -> bool:
    """Is this a well-formed Solana address? Checked before anything is sent."""
    try:
        from solders.pubkey import Pubkey  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise WalletError("Withdrawing needs the 'solders' package") from exc
    try:
        Pubkey.from_string(address.strip())
        return True
    except Exception:  # noqa: BLE001 - anything unparseable is simply not an address
        return False


def withdraw_sol(
    rpc: Any,
    keypair: Any,
    destination: str,
    lamports: Optional[int] = None,
    confirm_timeout: float = 60.0,
) -> Dict[str, Any]:
    """Send SOL out of the bot's wallet.

    `lamports=None` means everything, less the fee. This moves SOL only - any
    memecoins the bot still holds stay where they are, so close positions first
    if you want the whole balance out.
    """
    import base64
    import time

    try:
        from solders.hash import Hash  # type: ignore
        from solders.message import MessageV0  # type: ignore
        from solders.pubkey import Pubkey  # type: ignore
        from solders.system_program import TransferParams, transfer  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise WalletError("Withdrawing needs the 'solders' package") from exc

    destination = destination.strip()
    if not is_valid_address(destination):
        raise WalletError(f"{destination!r} is not a valid Solana address")

    sender = keypair.pubkey()
    if str(sender) == destination:
        raise WalletError("That is this wallet's own address")

    balance = rpc.get_balance_lamports(str(sender))
    if lamports is None:
        lamports = balance - TRANSFER_FEE_LAMPORTS
    lamports = int(lamports)

    if lamports <= 0:
        raise WalletError(
            f"Nothing to send: the wallet holds {balance / LAMPORTS_PER_SOL:.6f} SOL, "
            f"and {TRANSFER_FEE_LAMPORTS / LAMPORTS_PER_SOL:.6f} is needed for the fee"
        )
    if lamports + TRANSFER_FEE_LAMPORTS > balance:
        raise WalletError(
            f"Not enough SOL: sending {lamports / LAMPORTS_PER_SOL:.6f} plus fees needs more "
            f"than the {balance / LAMPORTS_PER_SOL:.6f} in the wallet"
        )

    blockhash = (rpc.get_latest_blockhash() or {}).get("blockhash")
    if not blockhash:
        raise WalletError("Could not fetch a recent blockhash from the RPC")

    instruction = transfer(
        TransferParams(from_pubkey=sender, to_pubkey=Pubkey.from_string(destination),
                       lamports=lamports)
    )
    message = MessageV0.try_compile(sender, [instruction], [], Hash.from_string(blockhash))
    signed = VersionedTransaction(message, [keypair])
    signature = rpc.send_raw_transaction(base64.b64encode(bytes(signed)).decode("utf-8"))

    deadline = time.time() + confirm_timeout
    confirmed, error = False, ""
    while time.time() < deadline:
        status = rpc.signature_status(signature)
        if status:
            if status.get("err"):
                error = f"transaction reverted: {status['err']}"
                break
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                confirmed = True
                break
        time.sleep(1.5)
    else:
        error = f"not confirmed within {confirm_timeout:.0f}s"

    return {
        "signature": str(signature),
        "confirmed": confirmed,
        "error": error,
        "lamports": lamports,
        "sol": lamports / LAMPORTS_PER_SOL,
        "destination": destination,
        "explorer": f"https://solscan.io/tx/{signature}",
    }


def env_file_has_key(env_path: str = ".env", key: str = ENV_KEY) -> bool:
    """True if `key` is already set (uncommented) in the env file."""
    path = Path(env_path)
    if not path.exists():
        return False
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*\S", re.MULTILINE)
    return bool(pattern.search(path.read_text()))


def append_to_env(values: Dict[str, str], env_path: str = ".env") -> None:
    """Append key=value lines to the env file, never overwriting what is there.

    Refuses if any key already has a value - silently replacing a wallet key
    would strand whatever funds the old one holds.
    """
    path = Path(env_path)
    for key in values:
        if env_file_has_key(env_path, key):
            raise WalletError(
                f"{key} is already set in {env_path}. Remove that line yourself if you "
                "really mean to replace it - refusing to overwrite a wallet key."
            )

    existing = path.read_text() if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    path.write_text(existing + lines)
    try:
        os.chmod(path, 0o600)  # best effort; a no-op on most Windows setups
    except OSError:  # pragma: no cover - platform dependent
        pass
