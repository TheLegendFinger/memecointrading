"""Logging setup shared by the CLI and the engine."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[38;5;244m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;196m",
        "CRITICAL": "\033[48;5;196;38;5;231m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = self.COLORS.get(record.levelname)
        if color and sys.stderr.isatty():
            return f"{color}{text}{self.RESET}"
        return text


def setup_logging(level: str = "INFO", log_file: Optional[str] = None,
                  console: bool = True) -> None:
    """Configure logging.

    `console=False` sends everything to the file only, which is what the live
    terminal display needs - log lines would otherwise scribble over the frame
    it redraws.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        # Close it too: the menu reconfigures logging on every run, and simply
        # dropping the handler leaks the log file's handle each time.
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - closing must never be fatal
            pass

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(
            _ColorFormatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")
        )
        root.addHandler(stream)

    if log_file:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        rotating = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        rotating.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s"))
        root.addHandler(rotating)

    if not console and not log_file:
        # Nothing would be recorded at all; keep warnings visible.
        fallback = logging.StreamHandler(sys.stderr)
        fallback.setLevel(logging.WARNING)
        fallback.setFormatter(_ColorFormatter("%(levelname)-7s %(message)s"))
        root.addHandler(fallback)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
