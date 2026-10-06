"""``trendlab keys``: show what the terminal actually sends for each key (and paste events).

Press keys; the last ten arrive as Textual sees them. Ctrl+V with an image on the clipboard tells
you whether the terminal passes the key through or swallows it. Esc or Ctrl+C exits.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key, Paste
from textual.widgets import Static

from trendlab.ui.theme import BLACK, NEON, NEON_DIM


class KeyProbe(App[None]):
    CSS = f"""
    Screen {{ background: {BLACK}; color: {NEON}; }}
    #log {{ padding: 1 2; }}
    """
    BINDINGS = [Binding("escape", "quit", "quit"), Binding("ctrl+c", "quit", "quit")]

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "TrendLab key probe — press keys (try Ctrl+V with an image on the clipboard, "
            "then Alt+V). Esc exits.\n",
            id="log",
        )

    def _show(self, line: str) -> None:
        self.seen.append(line)
        body = "\n".join(self.seen[-10:])
        self.query_one("#log", Static).update(
            f"[{NEON_DIM}]last keys as the terminal sends them:[/]\n{body}\n\n"
            f"[{NEON_DIM}]Esc exits[/]"
        )

    def on_key(self, event: Key) -> None:
        if event.key in {"escape"}:
            return
        self._show(f"key: {event.key!r}   character: {event.character!r}")

    def on_paste(self, event: Paste) -> None:
        text = event.text
        self._show(f"paste event: {len(text)} chars  {text[:40]!r}")


def main() -> None:
    KeyProbe().run()


if __name__ == "__main__":
    main()
