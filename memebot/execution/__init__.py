from .base import Executor
from .live import LiveExecutor

__all__ = ["Executor", "LiveExecutor", "build_executor"]


def build_executor(config, data=None, jupiter=None):
    """The executor. There is one: real swaps through Jupiter."""
    return LiveExecutor(config, jupiter=jupiter, data=data)
