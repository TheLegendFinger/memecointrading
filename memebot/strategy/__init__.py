from .base import Strategy
from .filters import CandidateFilter, FilterResult
from .momentum import MomentumStrategy

STRATEGIES = {"momentum": MomentumStrategy}


def build_strategy(name: str, config):
    """Instantiate a strategy by name."""
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy {name!r}. Available: {', '.join(sorted(STRATEGIES))}")
    return cls(config)


__all__ = ["Strategy", "CandidateFilter", "FilterResult", "MomentumStrategy", "build_strategy", "STRATEGIES"]
