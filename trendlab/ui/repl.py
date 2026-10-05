"""Rich-based interactive REPL (the plain terminal UI; the Textual TUI shares the same commands)."""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.markdown import Markdown

from trendlab import __version__
from trendlab.app import TrendLabApp
from trendlab.ui.commands import CommandRouter
from trendlab.ui.theme import BANNER, GREY, MINT, NEON


class Repl:
    def __init__(self, app: TrendLabApp, console: Console | None = None) -> None:
        self.app = app
        self.console = console or app.console
        self._running = True
        self._current: asyncio.Task | None = None
        self.commands = CommandRouter(app, self.console, quit_cb=self._quit)

    # -- entry point ------------------------------------------------------------------------
    async def run(self) -> None:
        await self.app.start(interactive=True, command_handler=self.handle_command)
        self._header()
        try:
            while self._running:
                self.console.print(f"[bold {MINT}]❯[/] ", end="")
                line = await self.app.console_input.readline()
                if line is None:
                    break
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    await self.handle_command(text)
                else:
                    await self._prompt(text)
        finally:
            await self.app.stop()

    def _header(self) -> None:
        status = self.app.remote_status()
        remote = (
            f"[bold {NEON}]remote ON[/] [{GREY}]{status['url']}[/]"
            if status["enabled"]
            else f"[{GREY}]remote off[/]"
        )
        resumed = f" · [{GREY}]resumed[/]" if self.app.resumed else ""
        mode_label = (
            "[unsafe] UNSAFE [/unsafe]"
            if self.app.engine.unsafe
            else f"[bold {NEON}]{self.app.engine.mode.value}[/]"
        )
        info = [
            f"[{GREY}]CLI v{__version__}[/]",
            f"[bold {NEON}]{self.app.model_ref}[/] [{GREY}]{self.app.privacy_label()}[/]",
            f"[{GREY}]{self.app.project_root}[/]",
            f"{mode_label} · {remote}{resumed}",
            f"[{GREY}]session {self.app.session_id} · type a task, or /help · Ctrl+C cancels[/]",
        ]
        for row, extra in zip(BANNER, info, strict=True):
            self.console.print(f"[bold {NEON}]{row}[/]   {extra}")
        self.console.print()

    async def _prompt(self, text: str) -> None:
        assert self.app.agent is not None
        self._current = asyncio.create_task(self.app.run_prompt(text))
        self.console.print(f"[{GREY}]type to steer · /stop interrupts[/]")
        reader: asyncio.Task | None = None
        try:
            while not self._current.done():
                if reader is None:
                    reader = asyncio.create_task(self.app.console_input.readline())
                done, _ = await asyncio.wait(
                    {self._current, reader}, return_when=asyncio.FIRST_COMPLETED
                )
                if reader in done:
                    line = reader.result()
                    reader = None
                    typed = (line or "").strip()
                    if not typed:
                        continue
                    if typed.lower() in {"/stop", "/esc", "/interrupt"}:
                        self.app.agent.cancel()
                        self._current.cancel()
                        self.console.print(
                            "[warning]■ Interrupted — type to continue the same session[/warning]"
                        )
                    elif typed.startswith("/"):
                        await self.handle_command(typed)
                    else:
                        self.app.agent.steer(typed)
                        self.console.print(
                            f"[accent]↳ steering:[/accent] {typed} "
                            f"[{GREY}](applied at the next turn)[/]"
                        )
            try:
                result = self._current.result()
            except asyncio.CancelledError:
                return
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
            self._current = None
        self.console.print()
        self.console.print(
            Markdown(result.report) if result.status == "COMPLETED" else result.report
        )

    def cancel_current(self) -> bool:
        if self._current is not None and not self._current.done():
            self.app.agent.cancel()  # type: ignore[union-attr]
            return True
        return False

    async def handle_command(self, text: str) -> None:
        await self.commands.dispatch(text)

    def _quit(self) -> None:
        self._running = False
