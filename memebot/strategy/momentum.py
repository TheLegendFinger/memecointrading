"""Momentum / volume-surge strategy.

The thesis: a memecoin worth buying is one that is *accelerating right now* -
5-minute price up, 1-hour trend intact, this hour's volume running hot versus
its own 24h average, and order flow tilted towards buyers - while still being
liquid enough to exit.

Every component is normalised to 0..1 and combined with configurable weights,
so the final score is comparable across candidates and easy to threshold.

Roughly what the composite means, so `strategy.min_score` can be chosen with
something other than a dart (see tests/test_momentum_calibration.py, which
pins these bands):

    0.90+  vertical - already up 40%+ on the hour, volume 4x its own average
    0.65   clearly running: +3-4% on 5m, +18% on 1h, 3x volume, 2:1 buys
    0.45   decent: moving up, hotter than usual, more buyers than sellers
    0.30   warming up
    <0.10  flat, drifting, or falling
"""

from __future__ import annotations

import logging
import math
from typing import Iterable, List, Optional

from ..config import StrategyConfig
from ..models import PairSnapshot, Position, Side, Signal
from .base import Strategy

log = logging.getLogger(__name__)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ramp(value: float, low: float, high: float) -> float:
    """Linear 0..1 ramp between `low` and `high`."""
    if high <= low:
        return 0.0
    return _clamp((value - low) / (high - low))


class MomentumStrategy(Strategy):
    name = "momentum"

    # Normalisation ramps. Each ceiling is "as good as this component measures",
    # so it has to be a number a real candidate actually reaches - set it to a
    # once-a-week moonshot and every ordinary pair scores near zero, which is
    # what made min_score unreachable in earlier versions. The anchors below
    # describe a coin that is clearly running, not a lottery winner.
    M5_FLOOR_PCT = 0.5
    M5_CEIL_PCT = 6.0    # +6% in five minutes is already a hard move
    H1_FLOOR_PCT = 2.0
    H1_CEIL_PCT = 30.0   # +30% on the hour
    SURGE_FLOOR = 1.0    # 1h volume equal to the 24h hourly average
    SURGE_CEIL = 3.5     # running 3.5x hot
    BUY_RATIO_FLOOR = 0.48
    BUY_RATIO_CEIL = 0.66  # two buys for every sell is lopsided already
    LIQ_FLOOR_USD = 25_000.0
    LIQ_CEIL_USD = 750_000.0

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

    # ---- score components ------------------------------------------------------
    def _momentum_m5(self, pair: PairSnapshot) -> float:
        return _ramp(pair.change("m5"), self.M5_FLOOR_PCT, self.M5_CEIL_PCT)

    def _momentum_h1(self, pair: PairSnapshot) -> float:
        return _ramp(pair.change("h1"), self.H1_FLOOR_PCT, self.H1_CEIL_PCT)

    def _volume_surge(self, pair: PairSnapshot) -> float:
        """How hot the last hour is versus this pair's own 24h hourly average."""
        hourly_avg = pair.vol("h24") / 24.0
        if hourly_avg <= 0:
            # No 24h baseline (brand new pair): fall back to 5m vs 1h.
            hour_from_m5 = pair.vol("m5") * 12.0
            if pair.vol("h1") <= 0:
                return 0.0
            return _ramp(hour_from_m5 / pair.vol("h1"), self.SURGE_FLOOR, self.SURGE_CEIL)
        return _ramp(pair.vol("h1") / hourly_avg, self.SURGE_FLOOR, self.SURGE_CEIL)

    def _buy_pressure(self, pair: PairSnapshot) -> float:
        """Blend of 5m and 1h buy share, weighted towards the fresher window."""
        short = pair.buy_ratio("m5") if pair.trades("m5") >= 5 else pair.buy_ratio("h1")
        blended = 0.6 * short + 0.4 * pair.buy_ratio("h1")
        return _ramp(blended, self.BUY_RATIO_FLOOR, self.BUY_RATIO_CEIL)

    def _liquidity_quality(self, pair: PairSnapshot) -> float:
        """Log-scaled: deeper pools are safer, with diminishing returns."""
        if pair.liquidity_usd <= self.LIQ_FLOOR_USD:
            return 0.0
        span = math.log(self.LIQ_CEIL_USD / self.LIQ_FLOOR_USD)
        return _clamp(math.log(pair.liquidity_usd / self.LIQ_FLOOR_USD) / span)

    # ---- Strategy API ----------------------------------------------------------
    def score(self, pair: PairSnapshot) -> float:
        cfg = self.cfg
        components = (
            (cfg.weight_momentum_m5, self._momentum_m5(pair)),
            (cfg.weight_momentum_h1, self._momentum_h1(pair)),
            (cfg.weight_volume_surge, self._volume_surge(pair)),
            (cfg.weight_buy_pressure, self._buy_pressure(pair)),
            (cfg.weight_liquidity, self._liquidity_quality(pair)),
        )
        total_weight = sum(w for w, _ in components)
        if total_weight <= 0:
            return 0.0
        raw = sum(w * v for w, v in components) / total_weight

        # Veto: never buy something that is actively bleeding on the 5m candle,
        # however good the rest of the picture looks.
        if pair.change("m5") <= 0:
            raw *= 0.5
        if pair.change("h1") < 0 and pair.change("m5") < 2.0:
            raw *= 0.6

        return _clamp(raw)

    def explain(self, pair: PairSnapshot) -> str:
        return (
            f"m5 {pair.change('m5'):+.1f}% h1 {pair.change('h1'):+.1f}% "
            f"vol1h ${pair.vol('h1'):,.0f} buys {pair.buy_ratio('h1') * 100:.0f}% "
            f"liq ${pair.liquidity_usd:,.0f}"
        )

    def generate_entries(self, pairs: Iterable[PairSnapshot]) -> List[Signal]:
        signals: List[Signal] = []
        for pair in pairs:
            value = self.score(pair)
            if value < self.cfg.min_score:
                continue
            signals.append(
                Signal(
                    token=pair.base,
                    side=Side.BUY,
                    score=value,
                    reason=f"momentum {value:.2f} | {self.explain(pair)}",
                    pair=pair,
                    price=pair.price_usd,
                )
            )
        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def should_exit(self, position: Position, pair: Optional[PairSnapshot]) -> Optional[str]:
        """Discretionary exit: the short-term move that got us in has reversed."""
        if pair is None:
            return None
        if pair.change("m5") <= self.cfg.exit_momentum_m5_pct:
            return f"momentum reversed ({pair.change('m5'):+.1f}% on 5m)"
        # Sellers taking over decisively while we are in profit-less limbo.
        if pair.trades("m5") >= 10 and pair.buy_ratio("m5") < 0.30 and pair.change("m5") < 0:
            return f"sell flow dominant ({pair.buy_ratio('m5') * 100:.0f}% buys on 5m)"
        return None
