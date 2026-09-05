"""Jupiter aggregator client - routing, quotes and swap transactions.

Jupiter is the venue we actually trade through on Solana. The same quote
endpoint is useful in paper mode too: it gives a realistic, route-aware
expected output (and price impact) for a given size.

Endpoints (v6 / price v2):
  GET  {quote_url}/quote
  POST {quote_url}/swap
  GET  {price_url}?ids=<mints>
  GET  https://tokens.jup.ag/token/<mint>     (decimals metadata)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from ..http import HttpClient, HttpError
from ..models import USDC_MINT, WSOL_MINT

log = logging.getLogger(__name__)

TOKEN_METADATA_URL = "https://tokens.jup.ag/token"


@dataclass
class JupiterQuote:
    """A routed quote. `raw` is passed back verbatim to POST /swap."""

    input_mint: str
    output_mint: str
    in_amount: int  # base units of input_mint
    out_amount: int  # base units of output_mint
    other_amount_threshold: int  # worst case out_amount at the given slippage
    price_impact_pct: float
    slippage_bps: int
    route_labels: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def hops(self) -> int:
        return len(self.route_labels)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_price_payload(data: Any) -> Dict[str, float]:
    """Read Jupiter's price response.

    The v2 shape nests results under "data" with a "price" field; v3 returns the
    mints at the top level with "usdPrice". Accept either so an upstream version
    bump does not silently zero out every price.
    """
    if not isinstance(data, dict):
        return {}
    entries = data.get("data") if isinstance(data.get("data"), dict) else data
    out: Dict[str, float] = {}
    for mint, entry in (entries or {}).items():
        if not isinstance(entry, dict):
            continue
        price = _to_float(entry.get("price")) or _to_float(entry.get("usdPrice"))
        if price > 0:
            out[str(mint)] = price
    return out


class JupiterClient:
    """Thin wrapper over the Jupiter APIs. Read-only; signing lives in the executor."""

    def __init__(
        self,
        quote_url: str = "https://lite-api.jup.ag/swap/v1",
        price_url: str = "https://lite-api.jup.ag/price/v2",
        timeout: float = 12.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        rate_limit_per_minute: int = 60,
        api_key: Optional[str] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.quote_url = quote_url.rstrip("/")
        self.price_url = price_url
        # A paid Jupiter key raises the rate limit; the keyless tier works fine
        # for a handful of orders a minute.
        self.api_key = api_key if api_key is not None else os.environ.get("JUPITER_API_KEY", "").strip()
        headers = {"x-api-key": self.api_key} if self.api_key else None
        self.http = http or HttpClient(
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            rate_limit_per_minute=rate_limit_per_minute,
            headers=headers,
        )
        self._decimals: Dict[str, int] = {WSOL_MINT: 9, USDC_MINT: 6}

    # ---- metadata --------------------------------------------------------------
    def decimals(self, mint: str, default: int = 9) -> int:
        """Token decimals, cached. Falls back to `default` if metadata is missing."""
        if mint in self._decimals:
            return self._decimals[mint]
        try:
            data = self.http.get(f"{TOKEN_METADATA_URL}/{mint}")
        except HttpError as exc:
            log.debug("Token metadata lookup failed for %s: %s", mint, exc)
            return default
        value = (data or {}).get("decimals")
        try:
            decimals = int(value)
        except (TypeError, ValueError):
            return default
        self._decimals[mint] = decimals
        return decimals

    def set_decimals(self, mint: str, decimals: int) -> None:
        self._decimals[mint] = int(decimals)

    def to_base_units(self, mint: str, amount: float) -> int:
        return int(round(amount * (10 ** self.decimals(mint))))

    def from_base_units(self, mint: str, amount: int) -> float:
        return float(amount) / (10 ** self.decimals(mint))

    # ---- prices ----------------------------------------------------------------
    def prices(self, mints: Iterable[str]) -> Dict[str, float]:
        """USD price per mint. Missing mints are simply absent from the result."""
        ids = [m for m in mints if m]
        if not ids:
            return {}
        out: Dict[str, float] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            try:
                data = self.http.get(self.price_url, params={"ids": ",".join(chunk)})
            except HttpError as exc:
                log.warning("Jupiter price request failed: %s", exc)
                continue
            for mint, price in _parse_price_payload(data).items():
                out[mint] = price
        return out

    def price(self, mint: str) -> float:
        return self.prices([mint]).get(mint, 0.0)

    # ---- routing ---------------------------------------------------------------
    def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 150,
        only_direct_routes: bool = False,
        max_accounts: Optional[int] = None,
    ) -> Optional[JupiterQuote]:
        """Route `amount` (base units of input_mint) into output_mint."""
        if amount <= 0:
            return None
        params: Dict[str, Any] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount),
            "slippageBps": int(slippage_bps),
            "onlyDirectRoutes": str(bool(only_direct_routes)).lower(),
        }
        if max_accounts:
            params["maxAccounts"] = int(max_accounts)
        try:
            data = self.http.get(f"{self.quote_url}/quote", params=params)
        except HttpError as exc:
            log.warning("Jupiter quote failed (%s -> %s): %s", input_mint[:6], output_mint[:6], exc)
            return None
        if not isinstance(data, dict) or not data.get("outAmount"):
            return None

        labels = []
        for step in data.get("routePlan") or []:
            info = (step or {}).get("swapInfo") or {}
            label = info.get("label") or info.get("ammKey") or ""
            if label:
                labels.append(str(label))

        return JupiterQuote(
            input_mint=str(data.get("inputMint") or input_mint),
            output_mint=str(data.get("outputMint") or output_mint),
            in_amount=int(data.get("inAmount") or amount),
            out_amount=int(data.get("outAmount")),
            other_amount_threshold=int(data.get("otherAmountThreshold") or 0),
            price_impact_pct=_to_float(data.get("priceImpactPct")) * 100.0,
            slippage_bps=int(data.get("slippageBps") or slippage_bps),
            route_labels=labels,
            raw=data,
        )

    def swap_transaction(
        self,
        quote: JupiterQuote,
        user_public_key: str,
        priority_fee_microlamports: int = 200_000,
        compute_unit_limit: Optional[int] = None,
        wrap_and_unwrap_sol: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Ask Jupiter to build the (unsigned) swap transaction for a quote."""
        body: Dict[str, Any] = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol,
            "dynamicComputeUnitLimit": compute_unit_limit is None,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(priority_fee_microlamports),
                    "priorityLevel": "high",
                }
            },
        }
        if compute_unit_limit:
            body["computeUnitLimit"] = int(compute_unit_limit)
        try:
            data = self.http.post(f"{self.quote_url}/swap", json_body=body)
        except HttpError as exc:
            log.error("Jupiter swap build failed: %s", exc)
            return None
        if not isinstance(data, dict) or not data.get("swapTransaction"):
            return None
        return data
