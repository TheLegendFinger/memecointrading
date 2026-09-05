"""Strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, List, Optional

from ..models import PairSnapshot, Position, Signal


class Strategy(ABC):
    """A strategy turns market snapshots into buy signals, and open positions
    into (optional) discretionary sell signals.

    Protective exits (stops, trailing stops, time limits) are the risk manager's
    job - a strategy only says "the thesis is over"."""

    name = "base"

    def __init__(self, config) -> None:
        self.cfg = config

    @abstractmethod
    def score(self, pair: PairSnapshot) -> float:
        """Return a 0..1 conviction score for entering `pair`."""

    @abstractmethod
    def generate_entries(self, pairs: Iterable[PairSnapshot]) -> List[Signal]:
        """Rank candidates and return buy signals, best first."""

    def should_exit(self, position: Position, pair: Optional[PairSnapshot]) -> Optional[str]:
        """Return a reason string if the entry thesis has broken, else None."""
        return None
