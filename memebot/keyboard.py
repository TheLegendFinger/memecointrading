"""Non-blocking line input, so a running loop can watch for a typed word.

The bot's live display redraws in place every second. A plain input() would
block that, and reading stdin from a background thread is worse: the thread
stays parked inside readline() after trading stops and swallows the next line
the user types at the menu.

So this polls instead. Nothing is read unless a whole line is waiting, and the
moment the loop ends, nothing is left holding stdin.

  POSIX    select() on stdin with a zero timeout - the line is already buffered
           by the terminal, so it arrives complete.
  Windows  select() does not work on stdin, so keystrokes are collected one at
           a time through msvcrt and assembled here.

Where stdin is not a terminal - piped input, a cron job, a service - `available`
is False and `poll()` always returns None, which is why the caller must keep a
signal handler as well.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


class LineReader:
    """Reads completed lines from the console without ever blocking."""

    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdin
        self.buffer = ""          # what has been typed so far, for display
        self._msvcrt = None
        self._select = None
        self.available = self._setup()

    def _setup(self) -> bool:
        try:
            if not self.stream.isatty():
                return False
        except Exception:  # noqa: BLE001 - a stream with no isatty is not a tty
            return False
        if sys.platform == "win32":
            try:
                import msvcrt
            except ImportError:  # pragma: no cover - only on Windows
                return False
            self._msvcrt = msvcrt
            return True
        try:
            import select
        except ImportError:  # pragma: no cover - POSIX always has it
            return False
        self._select = select
        return True

    def poll(self):
        """Return a completed line (without its newline), or None."""
        if not self.available:
            return None
        try:
            return self._poll_windows() if self._msvcrt else self._poll_posix()
        except Exception as exc:  # noqa: BLE001 - input is never worth crashing for
            log.debug("Could not read the console: %s", exc)
            self.available = False
            return None

    def _poll_posix(self):
        ready, _, _ = self._select.select([self.stream], [], [], 0)
        if not ready:
            return None
        line = self.stream.readline()
        if not line:                      # EOF: stop trying
            self.available = False
            return None
        return line.rstrip("\r\n")

    def _poll_windows(self):  # pragma: no cover - exercised by the fake on POSIX
        while self._msvcrt.kbhit():
            char = self._msvcrt.getwch()
            if char in ("\r", "\n"):
                line, self.buffer = self.buffer, ""
                return line
            if char == "\x08":            # backspace
                self.buffer = self.buffer[:-1]
            elif char == "\x03":          # Ctrl+C - let the signal handler have it
                raise KeyboardInterrupt
            elif char.isprintable():
                self.buffer += char
        return None
