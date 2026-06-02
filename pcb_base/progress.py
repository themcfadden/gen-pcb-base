from __future__ import annotations

import sys


class ProgressBar:
    """Minimal terminal progress bar for long-running stages."""

    def __init__(self, total: int, title: str) -> None:
        self.total = max(1, int(total))
        self.current = 0
        self.title = title
        self._last_render_len = 0
        self._render("starting")

    def advance(self, message: str = "") -> None:
        self.current = min(self.total, self.current + 1)
        self._render(message)
        if self.current >= self.total:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _render(self, message: str) -> None:
        width = 28
        ratio = self.current / self.total
        filled = int(width * ratio)
        bar = "#" * filled + "-" * (width - filled)
        percent = int(ratio * 100.0)
        suffix = f" {message}" if message else ""
        line = f"[{self.title}] [{bar}] {percent:3d}% ({self.current}/{self.total}){suffix}"
        pad = " " * max(0, self._last_render_len - len(line))
        sys.stderr.write(f"\r{line}{pad}")
        self._last_render_len = len(line)
        sys.stderr.flush()
