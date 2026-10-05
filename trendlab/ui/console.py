"""Non-blocking console input shared by the REPL and the local approval channel.

One daemon thread reads stdin lines and hands them to whichever coroutine is
currently awaiting ``readline()``. This keeps the terminal responsive while the
runtime waits on a remote approval.
"""

from __future__ import annotations

import asyncio
import sys
import threading


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

    def _reader(self) -> None:
        assert self._loop and self._queue is not None
        while True:
            try:
                line = sys.stdin.readline()
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
