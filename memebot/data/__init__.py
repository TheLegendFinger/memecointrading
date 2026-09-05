"""Market data clients, and the one place that turns a config into a scan."""

from typing import TYPE_CHECKING, List, Optional

from .dexscreener import DexScreenerClient
from .geckoterminal import GeckoTerminalClient
from .jupiter import JupiterClient, JupiterQuote

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import DataConfig
    from ..models import PairSnapshot


def build_gecko(cfg: "DataConfig", max_retries: int = 2) -> GeckoTerminalClient:
    return GeckoTerminalClient(
        base_url=cfg.geckoterminal_base_url,
        network=cfg.chain,
        timeout=cfg.request_timeout,
        max_retries=max_retries,
        backoff_seconds=cfg.backoff_seconds,
        rate_limit_per_minute=cfg.gecko_rate_limit_per_minute,
    )


def build_dexscreener(
    cfg: "DataConfig",
    max_retries: Optional[int] = None,
    cache_ttl_seconds: Optional[float] = None,
) -> DexScreenerClient:
    return DexScreenerClient(
        base_url=cfg.dexscreener_base_url,
        chain=cfg.chain,
        timeout=cfg.request_timeout,
        max_retries=cfg.max_retries if max_retries is None else max_retries,
        backoff_seconds=cfg.backoff_seconds,
        rate_limit_per_minute=cfg.rate_limit_per_minute,
        cache_ttl_seconds=(
            cfg.cache_ttl_seconds if cache_ttl_seconds is None else cache_ttl_seconds
        ),
        gecko=build_gecko(cfg),
    )


def discover_candidates(client: DexScreenerClient, cfg: "DataConfig") -> List["PairSnapshot"]:
    """Run one discovery pass for `cfg`.

    Every caller goes through here - the engine, `scan`, the health check and
    the deployment - so what the health check measures is what the bot trades.
    """
    return client.discover(
        cfg.search_terms,
        use_boosted_feed=cfg.use_boosted_feed,
        use_token_profiles=cfg.use_token_profiles,
        max_candidates=cfg.max_candidates,
        feed_limit=cfg.feed_limit,
        use_trending_pools=cfg.use_trending_pools,
        use_top_pools=cfg.use_top_pools,
        use_new_pools=cfg.use_new_pools,
        max_per_symbol=cfg.max_per_symbol,
    )


__all__ = [
    "DexScreenerClient",
    "GeckoTerminalClient",
    "JupiterClient",
    "JupiterQuote",
    "build_dexscreener",
    "build_gecko",
    "discover_candidates",
]
