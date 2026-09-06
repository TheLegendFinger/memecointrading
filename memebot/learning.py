"""Learning from the bot's own closed trades.

The bot records what it knew at the moment it bought - which feed found the
coin, how deep the pool was, how old it was, how hard it was moving - and, when
the position closes, what that trade returned. With enough of those it can tell
which kinds of entry have actually worked *for this bot, in this market* and
lean towards them.

The whole difficulty is that thirty memecoin trades is a tiny, violent sample.
Three lucky wins in a bucket is not evidence, and a naive win-rate table would
happily bet the wallet on it. So:

  * nothing is applied at all until `min_trades` trades have closed;
  * every bucket's edge is shrunk towards the overall average in proportion to
    how little data it has (n / (n + k)) - five trades barely move, fifty move
    most of the way;
  * the total adjustment is hard-capped, so this can tilt the strategy and
    never replace it. The cap is load-bearing rather than decorative: the
    buckets are correlated - a brand-new coin is usually also a thin one - so
    several of them say the same thing about the same trade and their
    contributions stack. Consistent evidence therefore reaches the cap
    quickly, which is the intended behaviour, not a bug to tune away;
  * `python -m memebot learn` prints every bucket, its sample size, its raw
    numbers and the adjustment, so nothing about it is hidden.

It is a tilt learned from experience, not a model. It cannot find a pattern
nobody wrote a bucket for, and it will happily learn a false one if the market
regime changes underneath it - which is why the cap exists.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import LearningConfig
from .models import PairSnapshot

log = logging.getLogger(__name__)


def _band(value: float, edges: List[float], labels: List[str]) -> str:
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


def entry_features(pair: PairSnapshot, source: str = "") -> Dict[str, str]:
    """The shape of a candidate at the moment of entry, as coarse buckets.

    Coarse on purpose: with a few dozen trades, fine-grained features would
    each have a sample of one.
    """
    liquidity = _band(
        pair.liquidity_usd,
        [50_000, 150_000, 500_000],
        ["liq<50k", "liq50-150k", "liq150-500k", "liq500k+"],
    )
    age_hours = pair.age_minutes / 60.0 if pair.age_minutes < 1e9 else 1e9
    age = _band(age_hours, [2, 12, 48], ["age<2h", "age2-12h", "age12-48h", "age48h+"])
    m5 = _band(pair.change("m5"), [1, 3, 8], ["m5<1%", "m5 1-3%", "m5 3-8%", "m5 8%+"])
    h1 = _band(pair.change("h1"), [5, 20, 60], ["h1<5%", "h1 5-20%", "h1 20-60%", "h1 60%+"])
    buys = _band(pair.buy_ratio("h1"), [0.5, 0.55, 0.62],
                 ["buys<50%", "buys50-55%", "buys55-62%", "buys62%+"])
    # UTC hour blocks. Liquidity and who is awake genuinely differ across them,
    # and three buckets stay dense enough to say something.
    hour = time.gmtime().tm_hour
    session = "session:asia" if hour < 7 else ("session:europe" if hour < 14 else "session:us")
    return {
        "source": f"found by {source or 'unknown'}",
        "liquidity": liquidity,
        "age": age,
        "m5": m5,
        "h1": h1,
        "buys": buys,
        "session": session,
    }


@dataclass
class BucketStat:
    dimension: str
    bucket: str
    trades: int
    wins: int
    mean_return: float      # fraction, e.g. 0.08 == +8%
    edge: float             # mean_return minus the overall mean
    adjustment: float       # what it contributes to a score, after shrinking

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


@dataclass
class LearningReport:
    trades: int = 0
    wins: int = 0
    mean_return: float = 0.0
    active: bool = False
    min_trades: int = 0
    buckets: List[BucketStat] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


class TradeLearner:
    """Turns closed trades into a small, capped tilt on the entry score."""

    def __init__(self, storage, cfg: LearningConfig) -> None:
        self.storage = storage
        self.cfg = cfg
        self._buckets: Dict[Tuple[str, str], BucketStat] = {}
        self._report = LearningReport(min_trades=cfg.min_trades)
        self._loaded_at = 0.0

    # ---- building ---------------------------------------------------------------
    def refresh(self, force: bool = False) -> LearningReport:
        """Recompute from the database, at most every `refresh_seconds`."""
        if not force and time.time() - self._loaded_at < self.cfg.refresh_seconds:
            return self._report
        try:
            rows = self.storage.closed_outcomes(limit=self.cfg.max_trades)
        except Exception as exc:  # noqa: BLE001 - learning must never break trading
            log.debug("Could not read trade outcomes: %s", exc)
            rows = []
        self._loaded_at = time.time()
        self._report = self._build(rows)
        self._buckets = {(b.dimension, b.bucket): b for b in self._report.buckets}
        return self._report

    def _build(self, rows: List[Dict[str, Any]]) -> LearningReport:
        report = LearningReport(min_trades=self.cfg.min_trades)
        returns = [float(r.get("return_pct") or 0.0) for r in rows]
        report.trades = len(rows)
        report.wins = sum(1 for r in returns if r > 0)
        report.mean_return = sum(returns) / len(returns) if returns else 0.0
        report.active = report.trades >= self.cfg.min_trades

        grouped: Dict[Tuple[str, str], List[float]] = {}
        for row in rows:
            features = row.get("features") or {}
            if not isinstance(features, dict):
                continue
            outcome = float(row.get("return_pct") or 0.0)
            for dimension, bucket in features.items():
                grouped.setdefault((dimension, str(bucket)), []).append(outcome)

        k = max(1.0, float(self.cfg.shrinkage_trades))
        for (dimension, bucket), values in sorted(grouped.items()):
            n = len(values)
            mean = sum(values) / n
            edge = mean - report.mean_return
            # Shrink towards "no opinion" by how thin the evidence is.
            shrunk = edge * (n / (n + k))
            adjustment = shrunk * self.cfg.sensitivity
            report.buckets.append(BucketStat(
                dimension=dimension,
                bucket=bucket,
                trades=n,
                wins=sum(1 for v in values if v > 0),
                mean_return=mean,
                edge=edge,
                adjustment=adjustment,
            ))
        report.buckets.sort(key=lambda b: abs(b.adjustment), reverse=True)
        return report

    # ---- applying ---------------------------------------------------------------
    def adjust(self, score: float, pair: PairSnapshot,
               source: str = "") -> Tuple[float, str]:
        """Return the tilted score and a short note about why.

        Never raises and never moves the score more than `max_adjustment`.
        """
        if not self.cfg.enabled:
            return score, ""
        report = self.refresh()
        if not report.active:
            return score, ""

        features = entry_features(pair, source or pair.source)
        total = 0.0
        contributions: List[Tuple[str, float]] = []
        for dimension, bucket in features.items():
            stat = self._buckets.get((dimension, str(bucket)))
            if stat is None or stat.trades < self.cfg.min_bucket_trades:
                continue
            total += stat.adjustment
            contributions.append((str(bucket), stat.adjustment))

        cap = self.cfg.max_adjustment
        total = max(-cap, min(cap, total))
        if abs(total) < 0.005:
            return score, ""

        contributions.sort(key=lambda c: abs(c[1]), reverse=True)
        why = ", ".join(f"{name} {value:+.2f}" for name, value in contributions[:2])
        return max(0.0, min(1.0, score + total)), f"learned {total:+.2f} ({why})"

    # ---- reporting --------------------------------------------------------------
    def report(self) -> LearningReport:
        return self.refresh(force=True)
