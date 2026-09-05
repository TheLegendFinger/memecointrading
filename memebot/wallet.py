"""Wallet helpers for live trading.

Creating a burner wallet, showing its address so you can fund it, and reading
its balances. Kept apart from the executor because these run before any trading
is armed - and because none of it should ever print a secret key by accident.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

ENV_KEY = "SOLANA_PRIVATE_KEY"
ENV_PATH_KEY = "SOLANA_KEYPAIR_PATH"


class WalletError(RuntimeError):
    pass


def _keypair_module():
    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise WalletError(
            "Creating a wallet needs the 'solders' package:\n"
            "  .\\.venv\\Scripts\\python.exe -m pip install -r requirements-live.txt"
        ) from exc
    return Keypair


def create_keypair() -> Tuple[str, str]:
    """Generate a brand-new wallet. Returns (public address, base58 secret)."""
    Keypair = _keypair_module()
    keypair = Keypair()
    return str(keypair.pubkey()), str(keypair)


def address_from_secret(secret: str) -> str:
    """Derive the public address from a base58 secret, validating it."""
    Keypair = _keypair_module()
    try:
        return str(Keypair.from_base58_string(secret.strip()).pubkey())
    except Exception as exc:
        raise WalletError(f"That is not a valid base58 secret key: {exc}") from exc


def configured_address() -> Optional[str]:
    """The address of whichever key the environment currently points at."""
    secret = os.environ.get(ENV_KEY, "").strip()
    if secret:
        return address_from_secret(secret)

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
