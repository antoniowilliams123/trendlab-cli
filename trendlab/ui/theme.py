"""TrendLab terminal theme: jet-black background, neon-green text.

One palette for both interfaces. The Rich ``Theme`` styles the REPL and every Rich renderable;
the Textual CSS below styles the full-screen TUI. Semantic names keep call sites readable
(``[ok]``, ``[warning]``, ``[danger]``) instead of raw colors.
"""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

BLACK = "#000000"
NEON = "#39ff14"  # primary text
NEON_DIM = "#1f9e12"  # borders, secondary text
NEON_SOFT = "#9dff8a"  # body text that should read softer than labels
MINT = "#00ffd0"  # accents (user prompt, links)
AMBER = "#ffd21f"  # approvals / warnings
RED = "#ff3b3b"  # denials / unsafe
GREY = "#5c6e5a"  # muted

RICH_THEME = Theme(
    {
        "neon": f"bold {NEON}",
        "soft": NEON_SOFT,
        "dim": GREY,
        "accent": f"bold {MINT}",
        "ok": f"bold {NEON}",
        "warning": f"bold {AMBER}",
        "danger": f"bold {RED}",
        "auto": f"bold black on {AMBER}",
        "user": f"bold {MINT}",
        "tool": NEON_DIM,
        "rule.line": NEON_DIM,
        "panel.border": NEON_DIM,
        "table.header": f"bold {NEON}",
        "table.border": NEON_DIM,
        "markdown.code": f"{MINT} on #0b1a0b",
        "markdown.h1": f"bold {NEON}",
        "markdown.h2": f"bold {NEON}",
        "markdown.item.bullet": NEON,
        "prompt": f"bold {MINT}",
        "repr.number": MINT,
        "repr.str": NEON_SOFT,
        "repr.path": MINT,
        "repr.filename": MINT,
        "progress.spinner": NEON,
        "status.spinner": NEON,
        "logging.level.warning": AMBER,
        "logging.level.error": RED,
    }
)

WORDMARK = "▌ T R E N D L A B"

# Big banner built from full blocks only (█), so it renders as solid pixels in every terminal font.
_GLYPHS = {
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "B": ["████ ", "█   █", "████ ", "█   █", "████ "],
}


def banner_rows(word: str = "TRENDLAB", gap: int = 2) -> list[str]:
    """Five rows of block letters for ``word`` (letters outside the glyph set are skipped)."""
    rows = ["" for _ in range(5)]
    for ch in word.upper():
        glyph = _GLYPHS.get(ch)
        if glyph is None:
            continue
        for i in range(5):
            rows[i] += (" " * gap if rows[i] else "") + glyph[i]
    return rows


BANNER = banner_rows()
BANNER_WIDTH = len(BANNER[0])


def make_console(**kwargs) -> Console:
    """A Rich console with the TrendLab theme and neon-on-black default style."""
    kwargs.setdefault("theme", RICH_THEME)
    kwargs.setdefault("style", f"{NEON_SOFT} on {BLACK}")
    kwargs.setdefault("highlight", False)
    return Console(**kwargs)


# Textual CSS shared by the TUI screens.
TUI_CSS = f"""
Screen {{
    background: {BLACK};
    color: {NEON};
}}
#header {{
    height: 6;
    background: {BLACK};
    color: {NEON};
    border-bottom: solid {NEON_DIM};
    padding: 0 1;
}}
#body {{
    height: 1fr;
    background: {BLACK};
}}
#transcript {{
    width: 3fr;
    background: {BLACK};
    color: {NEON_SOFT};
    border: round {NEON_DIM};
    border-title-color: {NEON};
    scrollbar-color: {NEON_DIM};
    scrollbar-color-hover: {NEON};
    scrollbar-background: {BLACK};
    padding: 0 1;
}}
#stream {{
    width: 3fr;
    height: auto;
    max-height: 12;
    background: {BLACK};
    color: {NEON};
    padding: 0 2;
    display: none;
}}
#stream.visible {{
    display: block;
}}
#left {{
    width: 3fr;
    height: 1fr;
}}
#plan {{
    width: 1fr;
    background: {BLACK};
    color: {NEON};
    border: round {NEON_DIM};
    border-title-color: {NEON};
    padding: 0 1;
}}
#status {{
    height: 1;
    background: #041004;
    color: {NEON};
    padding: 0 1;
}}
#statusline {{
    height: auto;
    max-height: 1;
    padding: 0 1;
    display: none;
}}
#input {{
    dock: bottom;
    height: auto;
    min-height: 3;
    max-height: 9;
    background: {BLACK};
    color: {NEON};
    border: tall {NEON_DIM};
}}
#input .text-area--cursor-line {{
    background: {BLACK};
}}
#input .text-area--cursor {{
    background: {NEON};
    color: {BLACK};
}}
#input:focus {{
    border: tall {NEON};
}}
Input > .input--placeholder {{
    color: {GREY};
}}
Input > .input--cursor {{
    background: {NEON};
    color: {BLACK};
}}
Footer {{
    background: {BLACK};
    color: {NEON_DIM};
}}
Footer > .footer--key {{
    background: #0b2a0b;
    color: {NEON};
}}
Footer > .footer--description {{
    color: {NEON_DIM};
}}
ApprovalModal {{
    align: center middle;
    background: {BLACK} 60%;
}}
#dialog {{
    width: 96;
    max-height: 90%;
    background: {BLACK};
    color: {NEON};
    border: double {AMBER};
    padding: 1 2;
}}
#dialog.question {{
    border: double {MINT};
}}
#dialog.high {{
    border: double {RED};
}}
#dialog Label {{
    color: {NEON};
    text-style: bold;
}}
#dialog Static {{
    color: {NEON_SOFT};
}}
#buttons {{
    height: 3;
    margin-top: 1;
}}
#buttons Button, #dialog Button {{
    margin-right: 1;
    background: {BLACK};
    color: {NEON};
    border: tall {NEON_DIM};
    text-style: bold;
}}
#dialog Button:hover, #dialog Button:focus {{
    border: tall {NEON};
    background: #0b2a0b;
}}
#dialog Button.-success {{
    color: {BLACK};
    background: {NEON};
    border: tall {NEON};
}}
#dialog Button.-error {{
    color: {RED};
    border: tall {RED};
}}
#dialog Button.-primary {{
    color: {MINT};
    border: tall {MINT};
}}
#preview {{
    height: auto;
    max-height: 22;
    border: round {NEON_DIM};
    scrollbar-color: {NEON_DIM};
    scrollbar-color-hover: {NEON};
    scrollbar-background: {BLACK};
}}
#dialog.models {{
    width: 112;
    height: auto;
    border: double {NEON};
}}
#model-filter {{
    background: {BLACK};
    color: {NEON};
    border: tall {NEON_DIM};
    margin-bottom: 1;
}}
#model-list {{
    height: auto;
    max-height: 14;
    background: {BLACK};
    color: {NEON};
    border: round {NEON_DIM};
}}
#model-list > .option-list--option-highlighted {{
    background: #0b2a0b;
    color: {NEON};
    text-style: bold;
}}
#model-detail {{
    margin-top: 1;
    color: {NEON_SOFT};
}}
#header.compact {{
    height: 2;
    border-bottom: solid {NEON_DIM};
}}
#cmdmenu {{
    display: none;
    height: auto;
    max-height: 12;
    background: {BLACK};
    color: {NEON};
    border: round {NEON_DIM};
    border-title-color: {NEON};
    margin: 0 1;
}}
#cmdmenu.visible {{
    display: block;
}}
#cmdmenu > .option-list--option-highlighted {{
    background: #0b2a0b;
    color: {NEON};
    text-style: bold;
}}
#picker {{
    display: none;
    height: auto;
    max-height: 10;
    background: {BLACK};
    color: {NEON};
    border: round {NEON_DIM};
    border-title-color: {NEON};
    margin: 0 1;
}}
#picker.visible {{
    display: block;
}}
#picker > .option-list--option-highlighted {{
    background: #0b2a0b;
    color: {NEON};
    text-style: bold;
}}
#picker > .option-list--option {{
    color: {NEON_SOFT};
}}
#answer {{
    background: {BLACK};
    color: {NEON};
    border: tall {MINT};
}}
"""
