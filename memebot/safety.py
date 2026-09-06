"""On-chain checks on the token itself, run before any money moves.

Everything else in this bot reads the *market*: price, volume, who is buying.
None of that can see the two things that empty a wallet fastest, because both
live on the token's mint account rather than in its chart:

  mint authority   still set means whoever deployed it can print more supply
                   whenever they like, diluting you to nothing in one
                   transaction. On a finished, honest token it is revoked.
  freeze authority still set means they can freeze your token account, so you
                   hold the coin and can never sell it. A chart can look
                   perfect right up to the moment you try to exit.

Then, holder concentration: if ten accounts hold most of the supply, the exit
is theirs and not yours, whatever the volume says.

These read the chain through the same RPC the bot trades with. Three calls per
token, cached, and only for a coin actually about to be bought - not for all
four hundred in a scan.

What this is NOT: a full rug scanner. It cannot see LP lock or burn (finding
the LP mint reliably needs per-DEX pool layouts), it cannot tell one owner
holding five accounts from five holders, and it says nothing about whether the
team will simply sell. It rules out two specific, mechanical disasters and
measures a third. Passing it is not a verdict that a coin is safe.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import SafetyConfig
from .models import PairSnapshot

log = logging.getLogger(__name__)


@dataclass
class TokenSafety:
    """The outcome of checking one token."""

    mint: str
    ok: bool
    checked: bool = True          # False when the chain could not be read
    reasons: List[str] = field(default_factory=list)   # why it failed
    mint_authority: Optional[str] = None
    freeze_authority: Optional[str] = None
    supply: float = 0.0
    top_holder_pct: float = 0.0   # biggest non-pool account, share of supply
    top10_pct: float = 0.0        # ten biggest non-pool accounts
    pool_pct: float = 0.0         # what sits in the pool, excluded from above

    @property
    def summary(self) -> str:
        if not self.checked:
            return "unverified"
        if not self.ok:
            return "; ".join(self.reasons)
        return (
            f"authorities revoked, top holder {self.top_holder_pct * 100:.0f}%, "
            f"top 10 {self.top10_pct * 100:.0f}%"
        )

    @property
    def badge(self) -> str:
        """One word for a table cell."""
        if not self.checked:
            return "?"
        return "ok" if self.ok else "RISK"


class TokenSafetyChecker:
    """Answers "is this token mechanically able to rug me" for one mint."""

    def __init__(self, rpc, cfg: SafetyConfig) -> None:
        self.rpc = rpc
        self.cfg = cfg
        self._cache: Dict[str, tuple] = {}

    def _cached(self, mint: str) -> Optional[TokenSafety]:
        entry = self._cache.get(mint)
        if not entry:
            return None
        stamp, value = entry
        if time.time() - stamp > self.cfg.cache_seconds:
            self._cache.pop(mint, None)
            return None
        return value

    def _pool_token_estimate(self, pair: PairSnapshot) -> float:
        """How many tokens the pool itself should be holding.

        DexScreener reports total pool liquidity in USD across both sides, so
        one side is about half of it. Used only to recognise the pool's own
        account among the largest holders - a constant-product pool is not a
        whale, and counting it as one makes every healthy token look captured.
        """
        if pair.price_usd <= 0 or pair.liquidity_usd <= 0:
            return 0.0
        return (pair.liquidity_usd / 2.0) / pair.price_usd

    def check(self, pair: PairSnapshot) -> TokenSafety:
        mint = pair.base.address
        cached = self._cached(mint)
        if cached is not None:
            return cached
        result = self._check_uncached(pair)
        self._cache[mint] = (time.time(), result)
        return result

    def _check_uncached(self, pair: PairSnapshot) -> TokenSafety:
        mint = pair.base.address
        try:
            info = self.rpc.get_mint_account(mint)
        except Exception as exc:  # noqa: BLE001 - a check must never kill a cycle
            log.warning("Could not read the mint account for %s: %s", mint, exc)
            return TokenSafety(mint=mint, ok=self.cfg.allow_unverified, checked=False,
                               reasons=[f"could not read the chain: {exc}"])

        mint_authority = info.get("mintAuthority") or None
        freeze_authority = info.get("freezeAuthority") or None
        result = TokenSafety(
            mint=mint, ok=True,
            mint_authority=mint_authority,
            freeze_authority=freeze_authority,
        )

        if self.cfg.require_mint_authority_revoked and mint_authority:
            result.reasons.append("mint authority still active - the supply can be inflated")
        if self.cfg.require_freeze_authority_revoked and freeze_authority:
            result.reasons.append("freeze authority still active - your tokens can be frozen")

        if self.cfg.max_top10_holder_pct > 0 or self.cfg.max_single_holder_pct > 0:
            self._measure_holders(pair, result)

        result.ok = not result.reasons
        return result

    def _measure_holders(self, pair: PairSnapshot, result: TokenSafety) -> None:
        mint = pair.base.address
        try:
            supply = float(self.rpc.get_token_supply(mint))
            amounts = list(self.rpc.get_token_largest_accounts(mint))
        except Exception as exc:  # noqa: BLE001 - as above; unreadable is not fatal
            log.debug("Could not read holders for %s: %s", mint, exc)
            if not self.cfg.allow_unverified:
                result.reasons.append(f"holders could not be read: {exc}")
            return
        if supply <= 0 or not amounts:
            if not self.cfg.allow_unverified:
                result.reasons.append("holders could not be read: empty response")
            return

        result.supply = supply
        pool_estimate = self._pool_token_estimate(pair)
        holders = list(amounts)
        if pool_estimate > 0:
            # Drop the one account that looks like the pool: the largest whose
            # balance is within a factor of two of what the pool should hold.
            for i, amount in enumerate(holders):
                if pool_estimate / 2.0 <= amount <= pool_estimate * 2.0:
                    result.pool_pct = amount / supply
                    holders.pop(i)
                    break

        if not holders:
            return
        result.top_holder_pct = holders[0] / supply
        result.top10_pct = sum(holders[:10]) / supply

        limit = self.cfg.max_single_holder_pct
        if limit > 0 and result.top_holder_pct > limit:
            result.reasons.append(
                f"one account holds {result.top_holder_pct * 100:.0f}% of supply "
                f"(limit {limit * 100:.0f}%)"
            )
        limit = self.cfg.max_top10_holder_pct
        if limit > 0 and result.top10_pct > limit:
            result.reasons.append(
                f"ten accounts hold {result.top10_pct * 100:.0f}% of supply "
                f"(limit {limit * 100:.0f}%)"
            )
