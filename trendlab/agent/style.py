"""Communication constraints (uplift U9): measure every final answer, enforce only what the
user configured.

Pure text metrics, no model call: prose word count (code blocks excluded), words per sentence,
Flesch reading ease, walls of text, filler phrases ("robospeak") and acronyms used without being
spelled out. ``check`` turns the metrics into issues against ``[governance]`` settings, and
``rules_text`` states the same settings in the system prompt, so the model is told the rules
before it writes and checked against them after.
"""

from __future__ import annotations

import re
from typing import Any

ROBOSPEAK = (
    "certainly!",
    "great question",
    "i hope this helps",
    "as an ai",
    "i'd be happy to",
    "i would be happy to",
    "feel free to",
    "let me know if you",
    "it's worth noting",
    "it is worth noting",
    "in conclusion",
    "delve",
    "rest assured",
    "absolutely!",
    "seamlessly",
    "robust and scalable",
    "leverage",
)
# acronyms a developer reads without help
COMMON_ACRONYMS = set(
    """API CLI CSV JSON HTTP HTTPS URL SQL UI OK ID IDs OS CPU GPU RAM PR CI TODO README HTML CSS
    JS TS UTF EOF IO PDF YAML TOML SSH AWS UTC ET LLM AI MD PY VCS XML DNS TCP UDP IP SDK IDE PEP
    NPM MCP""".split()
)
# shouted words and HTTP verbs are not acronyms
SHOUTED = set(
    """GET POST PUT PATCH HEAD NOTE TO DO NOT AND OR YES NO ALL NEW OLD FIX BUG WARN INFO DEBUG
    ERROR FAIL PASS DONE STOP ONLY MUST NEVER ASK THE IS IT IN ON OF AT BY PATH HOME GREEN
    RED""".split()
)
_CODE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`]*`")
_ACRONYM = re.compile(r"\b[A-Z]{2,5}s?\b")  # 6+ letters: a shouted word
_DEFINED = re.compile(r"\(([A-Z]{2,5})s?\)|\b([A-Z]{2,5})s? \(")


def _prose(text: str) -> str:
    return _INLINE.sub(" code ", _CODE.sub(" ", text or ""))


def _syllables(word: str) -> int:
    w = word.lower().strip(".,;:!?\"'()")
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def measure(text: str) -> dict[str, Any]:
    prose = _prose(text)
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", prose)
    sentences = [
        s for s in re.split(r"(?<=[.!?])\s+|\n\s*[-*\d]+[.)]?\s+|\n{2,}", prose) if s.strip()
    ]
    n_words = len(words)
    n_sent = max(1, len(sentences))
    syll = sum(_syllables(w) for w in words)
    ease = (
        round(206.835 - 1.015 * (n_words / n_sent) - 84.6 * (syll / n_words), 1)
        if n_words
        else None
    )
    paragraphs = [p for p in re.split(r"\n\s*\n", prose) if p.strip()]
    walls = sum(
        1
        for p in paragraphs
        if len(p.split()) > 120 and not re.search(r"^\s*([-*]|\d+[.)])\s", p, re.M)
    )
    low = prose.lower()
    filler = [p for p in ROBOSPEAK if p in low]
    defined = {a or b for a, b in _DEFINED.findall(prose)}
    acronyms = sorted(
        {
            a.rstrip("s")
            for a in _ACRONYM.findall(prose)
            if a.rstrip("s") not in COMMON_ACRONYMS | SHOUTED
            and a not in COMMON_ACRONYMS
            and a.rstrip("s") not in defined
        }
    )
    return {
        "words": n_words,
        "sentences": len(sentences),
        "words_per_sentence": round(n_words / n_sent, 1),
        "reading_ease": ease,
        "walls_of_text": walls,
        "robospeak": filler,
        "undefined_acronyms": acronyms,
    }


def configured(cfg: Any) -> bool:
    return bool(
        cfg is not None
        and (
            getattr(cfg, "max_answer_words", 0)
            or getattr(cfg, "plain_language", False)
            or getattr(cfg, "tone", "")
        )
    )


def check(metrics: dict[str, Any], cfg: Any) -> list[str]:
    """Issues against the configured constraints only (empty when nothing is configured)."""
    if not configured(cfg):
        return []
    issues = []
    limit = getattr(cfg, "max_answer_words", 0)
    if limit and metrics["words"] > limit:
        issues.append(f"the answer is {metrics['words']} words; the limit is {limit}")
    if getattr(cfg, "plain_language", False):
        if metrics["words_per_sentence"] > 25:
            issues.append(
                f"sentences average {metrics['words_per_sentence']} words; keep them under 20"
            )
        if metrics["undefined_acronyms"]:
            issues.append(
                "spell out " + ", ".join(metrics["undefined_acronyms"][:5]) + " on first use"
            )
        if metrics["walls_of_text"]:
            issues.append("break the long paragraph into short bullets")
    if metrics["robospeak"]:
        issues.append("drop filler phrases: " + ", ".join(f'"{p}"' for p in metrics["robospeak"]))
    return issues


def rules_text(cfg: Any) -> str:
    """System-prompt block stating the configured communication rules ('' when none)."""
    if not configured(cfg):
        return ""
    lines = ["", "## Communication rules (set by the user; your final answer is checked)"]
    if getattr(cfg, "max_answer_words", 0):
        lines.append(f"- Final answer: at most {cfg.max_answer_words} words outside code blocks.")
    if getattr(cfg, "plain_language", False):
        lines.append(
            "- Plain language: sentences under 20 words, one idea each; spell out an "
            "acronym the first time; short bullets instead of long paragraphs."
        )
    if getattr(cfg, "tone", ""):
        lines.append(f"- Tone: {cfg.tone}.")
    lines.append('- No filler ("Certainly!", "I hope this helps", "delve", "leverage").')
    return "\n".join(lines) + "\n"


def nudge(issues: list[str]) -> str:
    return (
        "Before you finish, rewrite only your final answer to meet the user's communication "
        "rules (do not redo the work):\n- " + "\n- ".join(issues)
    )
