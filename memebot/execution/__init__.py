from .base import Executor
from .paper import PaperExecutor

__all__ = ["Executor", "PaperExecutor", "build_executor"]


def build_executor(config, data=None, jupiter=None):
    """Create the executor for the configured mode.

    `live` is imported lazily so that paper trading never needs the Solana
    dependencies installed.
    """
    if config.mode == "live":
        from .live import LiveExecutor

        return LiveExecutor(config, jupiter=jupiter, data=data)
    return PaperExecutor(config, data=data, jupiter=jupiter)
