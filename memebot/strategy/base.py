"""Strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterable, List, Optional

from ..models import PairSnapshot, Position, Signal


class Strategy(ABC):
    """A strategy turns market snapshots into buy signals, and open positions
    into (optional) discretionary sell signals.

    Protective exits (stops, trailing stops, time limits) are the risk manager's
    job - a strategy only says "the thesis is over"."""

    name = "base"

    def __init__(self, config) -> None:
        self.cfg = config
        # Optional (score, pair) -> (score, why) hook. The learner plugs in
        # here so the threshold is applied to the tilted score, not the raw
        # one, and there is still only one place entries are generated.
        self.adjust_score: Optional[Callable[[float, PairSnapshot], tuple]] = None

    def tilt(self, score: float, pair: PairSnapshot) -> tuple:
        """Apply the score hook, if one is installed. Never raises."""
        if self.adjust_score is None:
            return score, ""
        try:
            return self.adjust_score(score, pair)
        except Exception:  # noqa: BLE001 - a tilt must never stop trading
            return score, ""

    @abstractmethod
    def score(self, pair: PairSnapshot) -> float:
        """Return a 0..1 conviction score for entering `pair`."""

    @abstractmethod
    def generate_entries(self, pairs: Iterable[PairSnapshot]) -> List[Signal]:
        """Rank candidates and return buy signals, best first."""

    def should_exit(self, position: Position, pair: Optional[PairSnapshot]) -> Optional[str]:
        """Return a reason string if the entry thesis has broken, else None."""
        return None
