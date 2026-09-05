"""Configuration loading.

Precedence (highest first):
  1. explicit CLI flags
  2. environment variables (MEMEBOT_* and the secrets in .env)
  3. the config file (YAML or JSON)
  4. the defaults below
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .models import Mode, USDC_MINT, WSOL_MINT
from .storage import resolve_state_target


@dataclass
class DataConfig:
    """Market data sources and discovery."""

    chain: str = "solana"
    # DexScreener search terms used to discover candidate pairs.
    search_terms: list = field(default_factory=lambda: ["SOL", "WSOL", "pump", "bonk"])
    # Also pull the boosted/trending token feeds.
    use_boosted_feed: bool = True
    max_candidates: int = 120
    request_timeout: float = 12.0
    max_retries: int = 3
    backoff_seconds: float = 1.0
    # Keep well under DexScreener's published 300 req/min limit.
    rate_limit_per_minute: int = 120
    cache_ttl_seconds: float = 5.0
    # Also sample the "newest tokens" and "latest boosts" feeds, not just search.
    use_token_profiles: bool = True
    dexscreener_base_url: str = "https://api.dexscreener.com"
    # Jupiter's keyless tier. With a paid key (JUPITER_API_KEY in the env) point
    # these at https://api.jup.ag/swap/v1 and https://api.jup.ag/price/v2.
    jupiter_quote_url: str = "https://lite-api.jup.ag/swap/v1"
    jupiter_price_url: str = "https://lite-api.jup.ag/price/v2"
    # Jupiter's free tier is stricter than DexScreener's, so it gets its own budget.
    jupiter_rate_limit_per_minute: int = 60


@dataclass
class FilterConfig:
    """Hard safety gates a pair must clear before it can be considered."""

    min_liquidity_usd: float = 25_000.0
    max_liquidity_usd: float = 5_000_000.0
    min_volume_h1_usd: float = 20_000.0
    min_volume_h24_usd: float = 100_000.0
    min_age_minutes: float = 20.0
    max_age_minutes: float = 60 * 24 * 14  # two weeks
    max_fdv_usd: float = 50_000_000.0
    min_market_cap_usd: float = 0.0
    min_trades_h1: int = 60
    min_buy_ratio_h1: float = 0.45
    # Reject pairs whose liquidity is tiny relative to their valuation - a
    # classic rug setup.
    min_liquidity_to_fdv: float = 0.015
    # Quote tokens we are willing to trade against.
    allowed_quote_mints: list = field(default_factory=lambda: [WSOL_MINT, USDC_MINT])
    allowed_dex_ids: list = field(default_factory=list)  # empty == any
    blacklist_mints: list = field(default_factory=list)
    blacklist_symbols: list = field(default_factory=list)
    # Do not chase something that already went vertical.
    max_price_change_h1_pct: float = 300.0
    max_price_change_h24_pct: float = 1500.0


@dataclass
class StrategyConfig:
    name: str = "momentum"
    # Minimum composite score (0..1) required to open a position.
    min_score: float = 0.55
    # Weights for the momentum score components.
    weight_momentum_m5: float = 0.30
    weight_momentum_h1: float = 0.25
    weight_volume_surge: float = 0.20
    weight_buy_pressure: float = 0.15
    weight_liquidity: float = 0.10
    # Exit when short-term momentum reverses this hard (percent).
    exit_momentum_m5_pct: float = -8.0
    max_new_positions_per_cycle: int = 2


@dataclass
class RiskConfig:
    starting_cash_usd: float = 1_000.0
    max_open_positions: int = 5
    # Size each entry as a fraction of current equity.
    position_size_pct: float = 0.08
    min_position_usd: float = 10.0
    max_position_usd: float = 250.0
    # Never take more than this share of the pool's liquidity.
    max_position_pct_of_liquidity: float = 0.005
    # Always keep some cash for fees/priority.
    cash_reserve_pct: float = 0.05

    stop_loss_pct: float = 0.20          # exit at -20%
    take_profit_pct: float = 0.60        # exit at +60%
    trailing_stop_pct: float = 0.25      # exit 25% off the high, once armed
    trailing_arm_profit_pct: float = 0.25  # arm the trailing stop at +25%
    max_hold_minutes: float = 60 * 12
    # Bail out if the pool drains relative to its peak while we are in it.
    liquidity_drain_pct: float = 0.5
    # Bail out if the pair disappears from market data for this long.
    stale_price_exit_minutes: float = 30.0

    max_daily_loss_pct: float = 0.15     # of the day's starting equity
    max_drawdown_pct: float = 0.35       # of peak equity, halts trading
    cooldown_minutes_after_loss: float = 30.0
    # Do not re-enter the same token for this long after exiting it.
    reentry_cooldown_minutes: float = 120.0


@dataclass
class ExecutionConfig:
    # Slippage tolerance sent to the aggregator / assumed when simulating.
    slippage_bps: int = 150
    # Swap fee charged by the venue (Raydium/Orca style pools ~0.25%).
    fee_bps: int = 25
    # Flat per-swap network + priority fee, in USD, used by the paper model.
    network_fee_usd: float = 0.05
    priority_fee_usd: float = 0.35
    # Extra simulated slippage on top of the size/liquidity impact model.
    paper_base_slippage_bps: int = 30
    # Paper fills fail this often, mimicking failed/expired Solana swaps.
    paper_failure_rate: float = 0.02
    # Ask Jupiter for a real route when simulating, instead of the built-in
    # constant-product impact model. More realistic, but needs network access.
    paper_use_live_quotes: bool = False
    # Deterministic paper fills for reproducible runs (null == random).
    paper_random_seed: object = None
    # Live only:
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    # SOL held back from trading to pay network fees and token-account rent.
    # Roughly 0.002 SOL per new token account plus fees, so this covers ~10 buys.
    sol_fee_reserve: float = 0.025
    priority_fee_microlamports: int = 200_000
    compute_unit_limit: int = 300_000
    max_tx_retries: int = 3
    confirm_timeout_seconds: float = 60.0
    quote_mint: str = WSOL_MINT


@dataclass
class BotConfig:
    mode: str = Mode.PAPER.value
    poll_interval_seconds: float = 30.0
    # A SQLite path, or a postgres:// URL for hosted/serverless deployments.
    state_db: str = "data/memebot.sqlite3"
    log_file: str = "logs/memebot.log"
    log_level: str = "INFO"
    dry_run: bool = False  # evaluate and log, never send orders
    data: DataConfig = field(default_factory=DataConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    # ---- helpers ---------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        errors = []
        if self.mode not in (Mode.PAPER.value, Mode.LIVE.value):
            errors.append(f"mode must be 'paper' or 'live', got {self.mode!r}")
        if self.poll_interval_seconds < 1:
            errors.append("poll_interval_seconds must be >= 1")
        r = self.risk
        if not 0 < r.position_size_pct <= 1:
            errors.append("risk.position_size_pct must be in (0, 1]")
        if r.min_position_usd > r.max_position_usd:
            errors.append("risk.min_position_usd cannot exceed risk.max_position_usd")
        if r.max_open_positions < 1:
            errors.append("risk.max_open_positions must be >= 1")
        if not 0 < r.stop_loss_pct < 1:
            errors.append("risk.stop_loss_pct must be in (0, 1)")
        if self.execution.slippage_bps < 0 or self.execution.slippage_bps > 5000:
            errors.append("execution.slippage_bps must be in [0, 5000]")
        if self.strategy.min_score < 0:
            errors.append("strategy.min_score must be >= 0")
        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))


# --------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------
def _merge_into(obj: Any, data: Dict[str, Any], path: str = "") -> None:
    """Recursively apply a plain dict onto a (nested) dataclass instance."""
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"Unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge_into(current, value, path=f"{path}{key}.")
        else:
            setattr(obj, key, value)


def _load_file(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError("PyYAML is required to read YAML config files") from exc
        return yaml.safe_load(text) or {}
    return json.loads(text or "{}")


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader; does not overwrite variables already in the env."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_ENV_OVERRIDES = {
    "MEMEBOT_MODE": ("mode", str),
    "MEMEBOT_POLL_INTERVAL": ("poll_interval_seconds", float),
    "MEMEBOT_LOG_LEVEL": ("log_level", str),
    "MEMEBOT_STARTING_CASH": ("risk.starting_cash_usd", float),
    "MEMEBOT_MAX_POSITIONS": ("risk.max_open_positions", int),
    "MEMEBOT_POSITION_SIZE_PCT": ("risk.position_size_pct", float),
    "MEMEBOT_SLIPPAGE_BPS": ("execution.slippage_bps", int),
    "MEMEBOT_RPC_URL": ("execution.rpc_url", str),
}


def _set_path(cfg: BotConfig, dotted: str, value: Any) -> None:
    target: Any = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> BotConfig:
    """Build a BotConfig from an optional file, the environment, and overrides."""
    load_dotenv()
    cfg = BotConfig()

    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        _merge_into(cfg, _load_file(p))

    for env_key, (dotted, caster) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is not None and raw != "":
            _set_path(cfg, dotted, caster(raw))

    # A database URL in the environment (MEMEBOT_STATE_DB, POSTGRES_URL,
    # DATABASE_URL...) beats the config file, so a deployment needs no edit.
    cfg.state_db = resolve_state_target(cfg.state_db)

    for dotted, value in (overrides or {}).items():
        if value is None:
            continue
        _set_path(cfg, dotted, value)

    cfg.validate()
    return cfg
