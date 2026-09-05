"""Executor interface shared by paper and live trading.

The engine only ever talks to this interface, which is what makes the paper
-> live switch a one-line config change rather than a rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Fill, Order


class Executor(ABC):
    """Turns an Order into a Fill."""

    mode = "abstract"

    @abstractmethod
    def execute(self, order: Order) -> Fill:
        """Execute an order. Implementations must never raise for ordinary
        failures - return a Fill with ok=False and an error message instead."""

    def price_for(self, token_address: str) -> float:
        """Best known USD price for a token, or 0.0 if unavailable."""
        return 0.0

    def describe(self) -> str:
        return self.mode

    def preflight(self) -> Optional[str]:
        """Return an error string if the executor is not ready to trade."""
        return None
