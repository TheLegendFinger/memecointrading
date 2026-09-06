"""Live execution on Solana through the Jupiter aggregator.

This is the real thing: it builds a swap route, signs a transaction with your
keypair and broadcasts it. It is deliberately hard to arm by accident -

  1. config `mode` must be `live`,
  2. `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK` must be set in the
     environment, and
  3. a wallet key must be supplied via `SOLANA_PRIVATE_KEY` (base58) or
     `SOLANA_KEYPAIR_PATH` (a Solana CLI JSON keypair file).

Requires the optional dependencies:  pip install -r requirements-live.txt
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import BotConfig
from ..http import HttpClient, HttpError
from ..models import Fill, Order, Side, WSOL_MINT
from .base import Executor

log = logging.getLogger(__name__)

CONFIRM_ENV = "LIVE_TRADING_CONFIRM"
CONFIRM_VALUE = "I_UNDERSTAND_THE_RISK"
LAMPORTS_PER_SOL = 1_000_000_000


class LiveExecutionError(RuntimeError):
    pass


# What Solana's JSON-RPC error codes actually mean, in words. Without this a
# failure reads as "{'code': -32002, 'message': ...}" - and once the activity
# feed has trimmed it, as "{'code': -3200".
RPC_ERROR_MEANINGS = {
    -32002: "the node simulated the transaction and it failed",
    -32003: "signature verification failed",
    -32004: "the block is not available yet",
    -32005: "the node is behind or rate-limiting this wallet",
    -32007: "that slot was skipped or is missing",
    -32009: "that slot was skipped or is missing",
    -32015: "the node does not support this transaction version",
    -32016: "the node has not caught up to the required slot",
    -32601: "the node does not offer this method",
    -32602: "the request was malformed",
}

# Anchor program errors start at 6000. Jupiter's 6001 is the one that matters:
# the price moved between the quote and the send.
CUSTOM_PROGRAM_ERRORS = {
    "0x1771": "slippage tolerance exceeded - the price moved between quote and send",
}


def _custom_program_error(text: str) -> str:
    for code, meaning in CUSTOM_PROGRAM_ERRORS.items():
        if code in text:
            return meaning
    if "insufficient lamports" in text or "InsufficientFunds" in text:
        return "not enough SOL to pay for the transaction"
    if "blockhash not found" in text.lower() or "BlockhashNotFound" in text:
        return "the blockhash expired before it landed - the network was busy"
    return ""


def is_preflight_failure(message: str) -> bool:
    """Whether the node rejected the transaction before forwarding it.

    This is the only case where trying again is provably safe: a simulation
    that fails means nothing reached the network, so there is nothing to
    double-spend. Anything that might already be in flight is left alone.
    """
    text = message.lower()
    return (
        "[-32002]" in text
        or "simulation failed" in text
        or "slippage tolerance exceeded" in text
    )


def describe_rpc_error(method: str, error: Any) -> str:
    """Turn a JSON-RPC error object into a sentence, meaning first.

    Meaning first on purpose: everything that displays this truncates, so the
    part that survives has to be the part worth reading.
    """
    if not isinstance(error, dict):
        return f"{method} failed: {error}"
    code = error.get("code")
    message = str(error.get("message") or "").strip()
    meaning = RPC_ERROR_MEANINGS.get(code, "")

    detail = message
    data = error.get("data")
    if isinstance(data, dict):
        logs = data.get("logs")
        if isinstance(logs, list) and logs:
            detail = f"{detail} | {logs[-1]}" if detail else str(logs[-1])
        elif data.get("err") is not None and not detail:
            detail = str(data["err"])

    cause = _custom_program_error(f"{message} {detail}")
    parts = [p for p in (cause or meaning, f"{method} [{code}]") if p]
    line = " - ".join(parts)
    if detail and detail not in line:
        line = f"{line}: {detail}"
    return line


def is_armed() -> bool:
    """Whether this process has acknowledged that orders spend real money."""
    return os.environ.get(CONFIRM_ENV, "") == CONFIRM_VALUE


def _load_keypair():
    """Load a solders Keypair from the environment. Never logs the secret."""
    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise LiveExecutionError(
            "Live trading needs the 'solders' package: pip install -r requirements-live.txt"
        ) from exc

    secret = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    if secret:
        try:
            return Keypair.from_base58_string(secret)
        except Exception as exc:
            raise LiveExecutionError(f"SOLANA_PRIVATE_KEY is not a valid base58 secret key: {exc}") from exc

    phrase = os.environ.get("SOLANA_MNEMONIC", "").strip()
    if phrase:
        from ..wallet import WalletError, keypair_from_mnemonic

        try:
            return keypair_from_mnemonic(phrase)
        except WalletError as exc:
            raise LiveExecutionError(f"SOLANA_MNEMONIC is not usable: {exc}") from exc

    path = os.environ.get("SOLANA_KEYPAIR_PATH", "").strip()
    if path:
        try:
            with open(os.path.expanduser(path), "r") as handle:
                data = json.load(handle)
            return Keypair.from_bytes(bytes(data))
        except Exception as exc:
            raise LiveExecutionError(f"Could not read keypair file {path}: {exc}") from exc

    raise LiveExecutionError(
        "No wallet found. Create one from the menu (Wallet), or set SOLANA_PRIVATE_KEY, "
        "SOLANA_MNEMONIC or SOLANA_KEYPAIR_PATH in .env."
    )


class SolanaRpc:
    """Just enough JSON-RPC for sending and confirming a swap."""

    def __init__(self, url: str, timeout: float = 20.0, http: Optional[HttpClient] = None) -> None:
        self.url = url
        self.http = http or HttpClient(timeout=timeout, max_retries=2, rate_limit_per_minute=600)
        self._id = 0

    def call(self, method: str, params: list) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            data = self.http.post(self.url, json_body=body)
        except HttpError as exc:
            # Every RPC call is a POST to the same URL, so without the method
            # name a failure reads as "POST https://... failed" and says
            # nothing about which call the node refused.
            raise HttpError(f"{method}: {exc}", exc.status) from exc
        if not isinstance(data, dict):
            raise HttpError(f"Unexpected RPC response for {method}")
        if "error" in data:
            raise HttpError(describe_rpc_error(method, data["error"]))
        return data.get("result")

    def get_balance_lamports(self, pubkey: str) -> int:
        result = self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int((result or {}).get("value") or 0)

    def get_token_balance(self, owner: str, mint: str) -> float:
        """Total UI balance of one SPL mint across the owner's token accounts."""
        result = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        total = 0.0
        for account in (result or {}).get("value") or []:
            info = (((account.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amount = (info.get("tokenAmount") or {}).get("uiAmount")
            if amount:
                total += float(amount)
        return total

    def get_mint_account(self, mint: str) -> Dict[str, Any]:
        """The SPL mint's own parsed state: authorities, supply, decimals.

        This is where the two questions that matter most are answered - can
        anyone still print more of this, and can anyone freeze yours.
        """
        result = self.call(
            "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
        data = (((result or {}).get("value") or {}).get("data") or {})
        if not isinstance(data, dict):
            return {}
        return ((data.get("parsed") or {}).get("info") or {})

    def get_token_supply(self, mint: str) -> float:
        result = self.call("getTokenSupply", [mint, {"commitment": "confirmed"}])
        return float(((result or {}).get("value") or {}).get("uiAmount") or 0.0)

    def get_token_largest_accounts(self, mint: str) -> List[float]:
        """The 20 biggest token accounts, largest first, as UI amounts.

        Token accounts, not owners: the RPC does not return who holds them, so
        one person spread across several accounts reads as several holders.
        It undercounts concentration; it never invents it.
        """
        result = self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        amounts = []
        for entry in (result or {}).get("value") or []:
            amount = (entry or {}).get("uiAmount")
            if amount:
                amounts.append(float(amount))
        amounts.sort(reverse=True)
        return amounts

    def get_latest_blockhash(self) -> Dict[str, Any]:
        result = self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return (result or {}).get("value") or {}

    def send_raw_transaction(self, signed_b64: str, max_retries: int = 3) -> str:
        return str(
            self.call(
                "sendTransaction",
                [
                    signed_b64,
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "maxRetries": int(max_retries),
                        "preflightCommitment": "confirmed",
                    },
                ],
            )
        )

    def signature_status(self, signature: str) -> Optional[Dict[str, Any]]:
        result = self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        values = (result or {}).get("value") or [None]
        return values[0]

    def get_transaction(self, signature: str) -> Optional[Dict[str, Any]]:
        return self.call(
            "getTransaction",
            [
                signature,
                {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
            ],
        )


class LiveExecutor(Executor):
    mode = "live"

    def __init__(self, config: BotConfig, jupiter=None, data=None, rpc: Optional[SolanaRpc] = None) -> None:
        self.config = config
        self.cfg = config.execution
        self.data = data
        self._keypair = None
        self._pubkey = ""

        if jupiter is None:
            from ..data.jupiter import JupiterClient

            jupiter = JupiterClient(
                quote_url=config.data.jupiter_quote_url,
                price_url=config.data.jupiter_price_url,
                timeout=config.data.request_timeout,
                max_retries=config.data.max_retries,
                rate_limit_per_minute=config.data.rate_limit_per_minute,
            )
        self.jupiter = jupiter
        self.rpc = rpc or SolanaRpc(self.cfg.rpc_url, timeout=config.data.request_timeout)

    # ---- arming / safety -------------------------------------------------------
    def preflight(self, require_arming: bool = True) -> Optional[str]:
        """Return a human-readable reason the executor cannot trade, or None."""
        if require_arming and not is_armed():
            return (
                f"live trading is not armed - set {CONFIRM_ENV}={CONFIRM_VALUE} "
                "in the environment to confirm you understand real funds are at risk"
            )
        try:
            self._ensure_wallet()
        except LiveExecutionError as exc:
            return str(exc)

        try:
            lamports = self.rpc.get_balance_lamports(self._pubkey)
        except HttpError as exc:
            return f"cannot reach RPC {self.cfg.rpc_url}: {exc}"

        sol = lamports / LAMPORTS_PER_SOL
        reserve = self.config.fee_reserve_sol
        if sol < reserve:
            return (
                f"wallet {self._pubkey} holds {sol:.4f} SOL, below the "
                f"{reserve:.4f} SOL it needs for fees and token-account rent - it "
                f"cannot pay for a swap. "
                "Send it more SOL."
            )
        return None

    def _ensure_wallet(self):
        if self._keypair is None:
            self._keypair = _load_keypair()
            self._pubkey = str(self._keypair.pubkey())
            log.info("Live wallet loaded: %s", self._pubkey)
        return self._keypair

    @property
    def wallet_address(self) -> str:
        self._ensure_wallet()
        return self._pubkey

    # ---- helpers ---------------------------------------------------------------
    def _exit_would_be_blocked(self, buy_quote) -> Optional[str]:
        """Ask what selling this position straight back would cost.

        DexScreener's reported liquidity is the whole pool; what matters is
        what the route can actually absorb, and the two part company on thin
        or fragmented pools. Quoting the round trip before committing is the
        difference between a trade and a donation - and it is the check that
        would have kept the bot out of the position it later could not sell.
        """
        ceiling = max(self.cfg.max_exit_slippage_bps, self.cfg.exit_slippage_bps)
        try:
            back = self.jupiter.quote(
                buy_quote.output_mint, buy_quote.input_mint,
                buy_quote.out_amount, ceiling,
            )
        except HttpError as exc:
            log.debug("Could not quote the way out: %s", exc)
            return None      # do not block a buy because a quote timed out
        if back is None:
            return "no route back out - the position could not be sold"
        if back.price_impact_pct > ceiling / 100.0:
            return (
                f"selling it back would cost {back.price_impact_pct:.2f}%, over the "
                f"{ceiling / 100.0:.2f}% exit ceiling - too thin to get out of"
            )
        return None

    def _quote_price_usd(self) -> float:
        """USD price of the quote currency we hold (SOL or a stablecoin)."""
        price = self.jupiter.price(self.cfg.quote_mint)
        if price > 0:
            return price
        if self.data is not None:
            price = self.data.price_usd(self.cfg.quote_mint)
        return price

    def sol_balance(self) -> float:
        return self.rpc.get_balance_lamports(self.wallet_address) / LAMPORTS_PER_SOL

    def available_cash_usd(self) -> Optional[float]:
        """What the wallet can actually spend on the next trade, in USD.

        In live mode this - not a number in a config file - is the bankroll.
        The fee reserve is held back so the wallet can always afford the
        network fees and token-account rent that a swap needs, and so a losing
        run cannot leave the wallet unable to sell what it holds.
        """
        try:
            quote_mint = self.cfg.quote_mint
            price = self._quote_price_usd()
            if price <= 0:
                return None

            if quote_mint == WSOL_MINT:
                spendable = max(0.0, self.sol_balance() - self.config.fee_reserve_sol)
                return spendable * price

            # Trading against a token (USDC): its balance is the bankroll, but
            # the wallet still needs SOL for fees.
            if self.sol_balance() < self.config.fee_reserve_sol:
                return 0.0
            return self.rpc.get_token_balance(self.wallet_address, quote_mint) * price
        except (HttpError, LiveExecutionError) as exc:
            log.warning("Could not read the wallet balance: %s", exc)
            return None

    def wallet_summary(self) -> Dict[str, Any]:
        """Everything the `wallet` command shows. Never includes the secret."""
        summary: Dict[str, Any] = {
            "address": self.wallet_address,
            "rpc_url": self.cfg.rpc_url,
            "quote_mint": self.cfg.quote_mint,
            "armed": is_armed(),
        }
        summary["sol_balance"] = self.sol_balance()
        summary["sol_price_usd"] = self.jupiter.price(WSOL_MINT)
        summary["sol_value_usd"] = summary["sol_balance"] * (summary["sol_price_usd"] or 0.0)
        summary["fee_reserve_sol"] = self.config.fee_reserve_sol
        summary["available_cash_usd"] = self.available_cash_usd()
        if self.cfg.quote_mint != WSOL_MINT:
            summary["quote_balance"] = self.rpc.get_token_balance(
                self.wallet_address, self.cfg.quote_mint
            )
        return summary

    def _sign_and_send(self, swap_tx_b64: str) -> str:
        from solders.transaction import VersionedTransaction  # type: ignore

        keypair = self._ensure_wallet()
        raw = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
        try:
            signed = VersionedTransaction(raw.message, [keypair])
        except Exception as exc:
            # solders refuses to sign when our key is not the transaction's
            # expected signer - which would mean the quote was built for a
            # different wallet. Fail clearly rather than sending junk.
            raise LiveExecutionError(
                f"refusing to send: this wallet ({self._pubkey}) is not the signer the "
                f"swap transaction expects ({exc})"
            ) from exc
        payload = base64.b64encode(bytes(signed)).decode("utf-8")
        return self.rpc.send_raw_transaction(payload, max_retries=self.cfg.max_tx_retries)

    def _confirm(self, signature: str) -> Tuple[bool, str]:
        deadline = time.time() + self.cfg.confirm_timeout_seconds
        while time.time() < deadline:
            try:
                status = self.rpc.signature_status(signature)
            except HttpError as exc:
                log.debug("Status poll failed: %s", exc)
                status = None
            if status:
                if status.get("err"):
                    return False, f"transaction reverted: {status['err']}"
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True, ""
            time.sleep(1.5)
        return False, f"not confirmed within {self.cfg.confirm_timeout_seconds:.0f}s"

    def _settled_amounts(self, signature: str, order: Order) -> Optional[Dict[str, float]]:
        """Read the confirmed transaction and derive what actually settled.

        Returns token/quote deltas for our wallet plus the network fee paid, or
        None when the transaction cannot be parsed (we then fall back to the
        quote's expected amounts).
        """
        try:
            tx = self.rpc.get_transaction(signature)
        except HttpError as exc:
            log.debug("getTransaction failed for %s: %s", signature, exc)
            return None
        meta = (tx or {}).get("meta") or {}
        if not meta:
            return None

        owner = self._pubkey
        token_mint = order.token.address
        quote_mint = self.cfg.quote_mint

        def balance_map(entries) -> Dict[str, float]:
            out: Dict[str, float] = {}
            for entry in entries or []:
                if entry.get("owner") != owner:
                    continue
                mint = entry.get("mint")
                amount = ((entry.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
                out[mint] = out.get(mint, 0.0) + float(amount)
            return out

        pre = balance_map(meta.get("preTokenBalances"))
        post = balance_map(meta.get("postTokenBalances"))
        token_delta = post.get(token_mint, 0.0) - pre.get(token_mint, 0.0)
        fee_lamports = float(meta.get("fee") or 0)

        # SOL side: use native balances when trading against wrapped SOL, since
        # Jupiter unwraps back into the account.
        if quote_mint == WSOL_MINT:
            accounts = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
            index = next(
                (i for i, key in enumerate(accounts)
                 if (key.get("pubkey") if isinstance(key, dict) else key) == owner),
                None,
            )
            pre_sol = (meta.get("preBalances") or [None])
            post_sol = (meta.get("postBalances") or [None])
            if index is not None and index < len(pre_sol) and index < len(post_sol):
                quote_delta = (post_sol[index] - pre_sol[index] + fee_lamports) / LAMPORTS_PER_SOL
            else:
                quote_delta = post.get(quote_mint, 0.0) - pre.get(quote_mint, 0.0)
        else:
            quote_delta = post.get(quote_mint, 0.0) - pre.get(quote_mint, 0.0)

        return {
            "token_delta": token_delta,
            "quote_delta": quote_delta,
            "fee_lamports": fee_lamports,
        }

    # ---- Executor API ----------------------------------------------------------
    def execute(self, order: Order) -> Fill:
        blocked = self.preflight()
        if blocked:
            return Fill(order=order, ok=False, error=blocked)

        attempts = 2 if self.cfg.requote_on_preflight_failure else 1
        for attempt in range(attempts):
            try:
                return self._execute(order)
            except LiveExecutionError as exc:
                return Fill(order=order, ok=False, error=str(exc))
            except HttpError as exc:
                message = str(exc)
                last = attempt == attempts - 1
                if last or not is_preflight_failure(message):
                    return Fill(order=order, ok=False, error=f"network error: {message}")
                # The node simulated it and refused, which means it never
                # forwarded it - nothing is on chain and nothing can be
                # double-spent by trying again. Almost always the price moved
                # between the quote and the send, so the fix is a fresh quote,
                # not a resend of the stale one.
                log.info("Re-quoting after a pre-flight refusal: %s", message)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Unexpected live execution failure")
                return Fill(order=order, ok=False, error=f"unexpected error: {exc}")
        return Fill(order=order, ok=False, error="could not place the order")

    def _execute(self, order: Order) -> Fill:
        quote_mint = self.cfg.quote_mint
        quote_price = self._quote_price_usd()
        if quote_price <= 0:
            return Fill(order=order, ok=False, error="cannot price the quote currency")

        token_mint = order.token.address
        slippage_bps = order.slippage_bps or self.cfg.slippage_bps

        if order.side is Side.BUY:
            in_mint, out_mint = quote_mint, token_mint
            in_amount = self.jupiter.to_base_units(quote_mint, order.usd_amount / quote_price)
        else:
            in_mint, out_mint = token_mint, quote_mint
            in_amount = self.jupiter.to_base_units(token_mint, order.token_amount)

        if in_amount <= 0:
            return Fill(order=order, ok=False, error="order size rounds to zero base units")

        quote = self.jupiter.quote(in_mint, out_mint, in_amount, slippage_bps)
        if quote is None:
            return Fill(order=order, ok=False, error="no route found")
        if quote.price_impact_pct > slippage_bps / 100.0:
            return Fill(
                order=order,
                ok=False,
                error=(
                    f"price impact {quote.price_impact_pct:.2f}% exceeds tolerance "
                    f"{slippage_bps / 100.0:.2f}%"
                ),
            )

        if order.side is Side.BUY and self.cfg.check_exit_route:
            blocked = self._exit_would_be_blocked(quote)
            if blocked:
                return Fill(order=order, ok=False, error=blocked)

        swap = self.jupiter.swap_transaction(
            quote,
            self.wallet_address,
            priority_fee_microlamports=self.cfg.priority_fee_microlamports,
            compute_unit_limit=self.cfg.compute_unit_limit or None,
        )
        if not swap:
            return Fill(order=order, ok=False, error="failed to build swap transaction")

        signature = self._sign_and_send(swap["swapTransaction"])
        log.info("Submitted %s swap: %s", order.side.value, signature)

        ok, error = self._confirm(signature)
        if not ok:
            return Fill(order=order, ok=False, tx_signature=signature, error=error)

        # Prefer what actually settled on chain; fall back to the quote.
        settled = self._settled_amounts(signature, order)
        expected_out = self.jupiter.from_base_units(out_mint, quote.out_amount)
        expected_in = self.jupiter.from_base_units(in_mint, quote.in_amount)

        if order.side is Side.BUY:
            token_amount = abs(settled["token_delta"]) if settled and settled["token_delta"] else expected_out
            quote_spent = abs(settled["quote_delta"]) if settled and settled["quote_delta"] else expected_in
        else:
            token_amount = abs(settled["token_delta"]) if settled and settled["token_delta"] else expected_in
            quote_spent = abs(settled["quote_delta"]) if settled and settled["quote_delta"] else expected_out

        usd_amount = quote_spent * quote_price
        if token_amount <= 0 or usd_amount <= 0:
            return Fill(
                order=order, ok=False, tx_signature=signature,
                error="confirmed but could not determine settled amounts",
            )

        price = usd_amount / token_amount
        fee_lamports = settled["fee_lamports"] if settled else 0.0
        sol_price = quote_price if quote_mint == WSOL_MINT else self.jupiter.price(WSOL_MINT)
        fee_usd = (fee_lamports / LAMPORTS_PER_SOL) * (sol_price or 0.0)

        realized_slip_bps = 0.0
        if order.reference_price > 0:
            delta = (price - order.reference_price) / order.reference_price
            realized_slip_bps = (delta if order.side is Side.BUY else -delta) * 10_000.0

        return Fill(
            order=order,
            ok=True,
            price=price,
            token_amount=token_amount,
            usd_amount=usd_amount,
            fee_usd=fee_usd,
            slippage_bps=realized_slip_bps,
            tx_signature=signature,
        )

    def price_for(self, token_address: str) -> float:
        price = self.jupiter.price(token_address)
        if price > 0:
            return price
        if self.data is not None:
            return self.data.price_usd(token_address)
        return 0.0

    def describe(self) -> str:
        try:
            wallet = self.wallet_address
        except LiveExecutionError:
            wallet = "<no key loaded>"
        return f"live via Jupiter (wallet {wallet}, rpc {self.cfg.rpc_url})"
