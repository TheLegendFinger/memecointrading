"""Terminal presentation helpers.

Deliberately dependency-free, and careful about two things Windows consoles get
wrong: ANSI colour needs enabling, and the classic console's code page cannot
encode box-drawing characters. Both are detected rather than assumed, so the
menu degrades to plain ASCII instead of crashing with UnicodeEncodeError.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

# ---- colour --------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

CYAN = "\033[38;5;39m"
GREEN = "\033[38;5;35m"
YELLOW = "\033[38;5;214m"
RED = "\033[38;5;203m"
GREY = "\033[38;5;244m"
WHITE = "\033[38;5;255m"

_ENABLED: Optional[bool] = None


def enable_ansi() -> bool:
    """Turn on ANSI escape handling; return whether colour is usable."""
    global _ENABLED
    if _ENABLED is not None:
        return _ENABLED

    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        _ENABLED = False
        return _ENABLED

    if os.name == "nt":
        # Windows 10+ supports ANSI, but the mode has to be set explicitly.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                )
        except Exception:  # noqa: BLE001 - an old console just gets no colour
            _ENABLED = False
            return _ENABLED

    _ENABLED = True
    return _ENABLED


def paint(text: str, *styles: str) -> str:
    if not enable_ansi() or not styles:
        return text
    return "".join(styles) + text + RESET


# ---- characters ----------------------------------------------------------------
def _encodable(sample: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        sample.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UNICODE_OK: Optional[bool] = None


def unicode_ok() -> bool:
    """Can this console print box-drawing characters without blowing up?"""
    global _UNICODE_OK
    if _UNICODE_OK is None:
        _UNICODE_OK = _encodable("─│╭╮╰╯·")
    return _UNICODE_OK


class Glyphs:
    """Box-drawing characters, with an ASCII fallback for old code pages."""

    def __init__(self, fancy: Optional[bool] = None) -> None:
        fancy = unicode_ok() if fancy is None else fancy
        self.h = "─" if fancy else "-"
        self.v = "│" if fancy else "|"
        self.tl = "╭" if fancy else "+"
        self.tr = "╮" if fancy else "+"
        self.bl = "╰" if fancy else "+"
        self.br = "╯" if fancy else "+"
        self.dot = "·" if fancy else "*"
        self.arrow = "›" if fancy else ">"
        self.up = "▲" if fancy else "^"
        self.down = "▼" if fancy else "v"


# The wordmark. Heavy box-drawing characters, so it needs the same encoding
# check everything else does - a console on an old code page gets the plain
# version rather than a UnicodeEncodeError.
WORDMARK = [
    "╋╋╋╋╋╋╋╋╋╋┏┓╋╋┏┓",
    "┏━━┳━┳━━┳━┫┗┳━┫┗┓",
    "┃┃┃┃┻┫┃┃┃┻┫╋┃╋┃┏┫",
    "┗┻┻┻━┻┻┻┻━┻━┻━┻━┛",
]

WORDMARK_ASCII = [
    " _ _  _ _ _  _ ___  __  ___",
    "| ' \/ ' ' \/ '| _ \/ _ \|_ _|",
    "|_|_|\_|_|_|\_|_|___/\___/ |_|",
]


def wordmark(fancy: Optional[bool] = None) -> List[str]:
    """The memebot wordmark, in whichever character set this console can print."""
    fancy = unicode_ok() if fancy is None else fancy
    lines = WORDMARK if fancy else WORDMARK_ASCII
    if fancy:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            "".join(WORDMARK).encode(encoding)
        except (UnicodeEncodeError, LookupError):
            lines = WORDMARK_ASCII
    width = max(len(line) for line in lines)
    return [line.ljust(width) for line in lines]


def clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    # ANSI clear + home. Works on Windows once VT processing is on.
    if enable_ansi():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    else:  # pragma: no cover - only very old consoles
        os.system("cls" if os.name == "nt" else "clear")


def visible_length(text: str) -> int:
    """Length ignoring ANSI escape sequences, for padding inside boxes."""
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            end = text.find("m", i)
            if end == -1:
                break
            i = end + 1
            continue
        out += 1
        i += 1
    return out


def box(title: str, right: str = "", width: int = 66, glyphs: Optional[Glyphs] = None) -> str:
    """A single-line titled box, with optional right-aligned text."""
    g = glyphs or Glyphs()
    inner = width - 2
    left_text = f" {title}"
    right_text = f"{right} " if right else " "
    pad = inner - visible_length(left_text) - visible_length(right_text)
    pad = max(1, pad)
    return (
        f"{g.tl}{g.h * inner}{g.tr}\n"
        f"{g.v}{left_text}{' ' * pad}{right_text}{g.v}\n"
        f"{g.bl}{g.h * inner}{g.br}"
    )


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float) -> str:
    return f"{value:+.2f}%"
