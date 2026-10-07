"""Non-blocking console input shared by the REPL and the local approval channel.

One daemon thread reads stdin lines and hands them to whichever coroutine is
currently awaiting ``readline()``. This keeps the terminal responsive while the
runtime waits on a remote approval.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path


class ConsoleInput:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def interactive(self) -> bool:
        try:
            return sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(target=self._reader, name="trendlab-stdin", daemon=True)
        self._thread.start()

    def enable_history(self, path: Path) -> bool:
        """Arrow-key history and line editing for the plain REPL via GNU readline (spec §92.5).

        Returns False when readline is unavailable or stdin is not a terminal; the reader then
        falls back to plain line reads.
        """
        if not self.interactive:
            return False
        try:
            import readline
        except ImportError:
            return False
        self._history_path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                readline.read_history_file(str(path))
            readline.set_history_length(500)
        except OSError:
            pass
        self._readline = readline
        return True

    def save_history(self) -> None:
        rl = getattr(self, "_readline", None)
        path = getattr(self, "_history_path", None)
        if rl is None or path is None:
            return
        try:
            rl.write_history_file(str(path))
        except OSError:
            pass

    def _read_one(self) -> str:
        """One line from the terminal: ``input()`` when readline is active (arrows, editing,
        history), otherwise a plain read."""
        if getattr(self, "_readline", None) is not None:
            try:
                return input() + "\n"
            except EOFError:
                return ""
        return sys.stdin.readline()

    def _reader(self) -> None:
        assert self._loop and self._queue is not None
        while True:
            try:
                line = self._read_one()
            except (ValueError, OSError):
                line = ""
            if line == "":
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
                return
            self._loop.call_soon_threadsafe(self._queue.put_nowait, line.rstrip("\r\n"))

    async def readline(self) -> str | None:
        """Return the next line, or None on EOF."""
        if self._queue is None:
            self.start()
        assert self._queue is not None
        return await self._queue.get()
