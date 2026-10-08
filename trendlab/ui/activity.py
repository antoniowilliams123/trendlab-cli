"""One renderer for agent activity lines, shared by the TUI and the plain REPL (spec §91.1).

Every tool attempt leaves a visible trace — started, finished (with a preview of what came
back), or skipped (denied, invalid arguments, unknown tool, blocked by a hook, not approved) —
so the person always sees what the agent did and why something did not happen. Returned strings
are Rich markup in the neon theme.
"""

from __future__ import annotations

import re

from rich.markup import escape

from trendlab.telemetry.events import Event, EventType
from trendlab.ui.theme import GREY, MINT, NEON, NEON_DIM, RED

AMBER = "#ffd21f"

_SKIP_LABEL = {  # BMP glyphs only: emoji render as boxes or double-width in some terminals
    "denied": "✗ denied",
    "not_approved": "✗ not approved",
    "invalid_args": "⚠ invalid arguments",
    "unknown_tool": "⚠ unknown tool",
    "malformed": "⚠ malformed call",
    "hook_blocked": "✗ blocked by hook",
    "plan_rejected": "✗ plan rejected",
    "checkpoint_failed": "✗ checkpoint failed",
}


def _short(text: str | None, limit: int = 110) -> str:
    text = (text or "").strip().replace("\n", " ⏎ ")
    return escape(text if len(text) <= limit else text[: limit - 1] + "…")


def format_event(event: Event) -> str | None:
    """Rich-markup line for an activity event, or None when the UI should not print it."""
    d = event.data
    t = event.type
    if t == EventType.TOOL_STARTED:
        summary = str(d.get("summary") or d.get("tool"))
        detail = d.get("detail") or d.get("command") or ", ".join(d.get("files") or [])
        if detail and detail in summary:
            detail = ""  # "Read pyproject.toml" already says it
        head = f"[{NEON_DIM}]●[/] [{NEON}]{escape(summary)}[/]"
        return head + (f"  [{GREY}]{_short(detail)}[/]" if detail else "")
    if t == EventType.TOOL_COMPLETED:
        ms = d.get("duration_ms", 0)
        if d.get("ok"):
            extra = ""
            lines = d.get("lines")
            if lines:
                extra = f" · {lines} line{'s' if lines != 1 else ''}"
            preview = "" if d.get("tool") == "ask_user" else d.get("preview")
            tail = f"\n    [{GREY}]{_short(preview, 140)}[/]" if preview else ""
            return (
                f"  [bold {NEON}]✓[/] [{NEON_DIM}]{d.get('tool')}[/] [{GREY}]{ms} ms{extra}[/]"
                + tail
            )
        err = d.get("error") or ""
        code = d.get("exit_code")
        code_txt = f" · exit {code}" if code not in (None, 0) else ""
        return f"  [bold {RED}]✗[/] [{NEON_DIM}]{d.get('tool')}[/] [{GREY}]{ms} ms{code_txt}[/]" + (
            f"\n    [{RED}]{_short(err, 160)}[/]" if err else ""
        )
    if t == EventType.TOOL_SKIPPED:
        label = _SKIP_LABEL.get(str(d.get("reason")), "⚠ skipped")
        what = d.get("detail") or d.get("command") or ""
        message = "" if d.get("reason") == "not_approved" else d.get("message")  # denial line above
        return (
            f"  [bold {RED}]{label}[/] [{NEON_DIM}]{escape(str(d.get('tool')))}[/]"
            + (f" [{GREY}]{_short(what)}[/]" if what else "")
            + (f"\n    [{GREY}]{_short(message, 160)}[/]" if message else "")
        )
    if t == EventType.TOOL_OUTPUT_TIERED:
        return (
            f"  [{GREY}]⊟ {d.get('tool')} output {d.get('raw_chars')} → {d.get('shown_chars')} "
            f"chars ({d.get('parser')}); full text kept for inspect_output[/]"
            + (f"\n    [{AMBER}]{escape(str(d.get('anomaly')))}[/]" if d.get("anomaly") else "")
        )
    if t == EventType.PLANNER_CALLED:
        n = d.get("steps") or 0
        if not n:
            return f"[{GREY}]▤ planner call {d.get('call')}: no usable plan[/]"
        titles = "; ".join(str(x) for x in (d.get("titles") or [])[:4])
        word = "re-planned" if d.get("replan") else "planned"
        return f"[{MINT}]▤ {word} {n} steps[/] [{GREY}]{escape(_short(titles, 160))}[/]"
    if t == EventType.STEP_STARTED:
        return f"[{NEON_DIM}]▸ step {d.get('step')}[/] [{GREY}]{escape(_short(str(d.get('title')), 100))}[/]"
    if t == EventType.STEP_COMPLETED:
        via = " via best-of-N" if d.get("via") == "best_of" else ""
        return f"[{NEON}]▪ step {d.get('step')} done{via} ✓[/]"
    if t == EventType.STEP_FAILED:
        why = {
            "iteration_cap": f"hit the {d.get('cap')}-iteration cap",
            "validation_failed": "validation failed",
            "best_of_exhausted": "no parallel candidate passed",
        }.get(str(d.get("reason")), str(d.get("reason")))
        return f"[{AMBER}]▪ step {d.get('step')} attempt {d.get('attempt')}: {why}[/]"
    if t == EventType.ATTEMPT_CANDIDATE:
        mark = f"[{NEON}]passed[/]" if d.get("passed") else f"[{GREY}]failed[/]"
        return (
            f"  [{GREY}]⑂ candidate {d.get('n')}:[/] {mark} [{GREY}]{d.get('diff_lines')} diff lines"
            f" · ${d.get('cost_usd', 0):.3f} · {d.get('elapsed_s', 0):.0f}s[/]"
        )
    if t == EventType.SKILL_LOADED:
        return f"[{MINT}]◈ skill {escape(str(d.get('name')))} loaded[/] [{GREY}]({d.get('trigger')})[/]"
    if t == EventType.VERIFY_STARTED:
        return f"[{MINT}]⚖ verifying the change before reporting it done[/]"
    if t == EventType.VERIFY_VERDICT:
        v = str(d.get("verdict"))
        n = d.get("findings") or 0
        model = f" [{GREY}]{escape(str(d.get('model') or ''))}[/]" if d.get("model") else ""
        if v == "pass":
            return f"[bold {NEON}]⚖ verified ✓[/]{model}"
        if v == "fix":
            return f"[{AMBER}]⚖ verifier asked for fixes ({n})[/]{model}"
        if v == "fail":
            return f"[bold {RED}]⚖ verifier rejected the change ({n} findings)[/]{model}"
        return f"[{GREY}]⚖ verifier unavailable[/]"
    if t == EventType.REGRESSION_GATE:
        o = str(d.get("outcome"))
        label = {
            "present": f"[{NEON_DIM}]⊕ regression test present[/]",
            "waived": f"[{AMBER}]⊕ regression test waived by the model[/]",
            "missing": f"[{AMBER}]⊕ no regression test for a fix[/]",
        }.get(o)
        return label
    if t == EventType.WORKTREE_RUN:
        o = str(d.get("outcome"))
        if o == "entered":
            return f"[{GREY}]⎇ working in a throwaway worktree ({escape(str(d.get('name')))})[/]"
        if o == "applied":
            return f"[{NEON_DIM}]⎇ verified diff applied to the working tree[/]"
        if o == "parked":
            return f"[{AMBER}]⎇ diff parked as {escape(str(d.get('patch')))} (not applied)[/]"
        if o == "skipped":
            return f"[{GREY}]⎇ worktree mode skipped: {escape(str(d.get('reason')))}[/]"
        return None
    if t == EventType.DIAGNOSTICS:
        report = str(d.get("report") or "")
        body = [ln for ln in report.splitlines()[1:] if ln.strip()][:3]
        shown = "\n".join(f"    [{GREY}]{_short(ln, 140)}[/]" for ln in body)
        return (
            f"  [{AMBER}]✎ diagnostics after edit[/] [{GREY}]{', '.join(d.get('files') or [])}[/]"
            + ("\n" + shown if shown else "")
        )
    if t == EventType.TOOLS_PARALLEL:
        return f"[{GREY}]⇉ {d.get('count')} read-only tools in parallel[/]"
    if t == EventType.PERMISSION_REQUESTED:
        return None  # the approval event that follows carries the useful text
    if t == EventType.APPROVAL_REQUESTED:
        return f"[bold {AMBER}]⏳ waiting for approval[/] [{NEON_DIM}]{_short(d.get('summary'))}[/]"
    if t == EventType.APPROVAL_REQUESTED and d.get("kind") == "question":
        return None
    if t == EventType.APPROVAL_APPROVED:
        if d.get("kind") == "question":
            return None  # the answer line is printed by the UI that collected it
        where = {"web": "from the phone", "telegram": "from Telegram"}.get(
            str(d.get("channel")), "at the keyboard"
        )
        return f"[bold {NEON}]✓ approved {where}[/] [{GREY}]— continuing[/]"
    if t == EventType.APPROVAL_DENIED:
        where = {"web": "from the phone", "telegram": "from Telegram"}.get(
            str(d.get("channel")), "at the keyboard"
        )
        return f"[bold {RED}]✗ denied {where}[/]"
    if t == EventType.APPROVAL_EXPIRED:
        return f"[bold {AMBER}]⌛ approval expired[/]"
    if t == EventType.SECRET_WRITE_BLOCKED:
        return f"[bold {RED}]✗ blocked a write that looked like a secret[/] [{GREY}]{escape(str(d.get('files')))}[/]"
    if t == EventType.CONTEXT_COMPACTED:
        return (
            f"[{AMBER}]⇅ compacted {d.get('messages_compacted')} messages[/] "
            f"[{GREY}]{d.get('tokens_before')} → {d.get('tokens_after')} tokens[/]"
        )
    if t == EventType.RECOVERY:
        failure = str(d.get("failure") or "")
        if failure == "TOOL_CALL_AS_TEXT":
            return (
                f"[{AMBER}]↳ the model wrote a tool call as text — running it:[/] "
                f"[{GREY}]{', '.join(d.get('tools') or [])}[/]"
            )
        if failure == "UNVERIFIED_COMPLETION":
            return f"[{AMBER}]✎ asked the model to validate its work before finishing[/]"
        if d.get("action") == "compact":
            return f"[{AMBER}]⇅ context overflow — compacting and retrying[/]"
        if d.get("action") == "escalate":
            why = "quality" if failure == "UNVERIFIED_COMPLETION" else "no progress"
            return (
                f"[{AMBER}]⤴ {why} — handing the rest of this run to {d.get('to_model')}[/] "
                f"[{GREY}](back to {d.get('from_model')} after)[/]"
            )
        if d.get("action") == "restored":
            return f"[{GREY}]⤵ back to {d.get('to_model')}[/]"
        if failure == "AUTH_FAILURE":
            err = str(d.get("error") or "")
            m = re.search(r"variable (\w+)", err)
            hint = f" → run: trendlab secret set {m.group(1)}" if m else " → check the provider key"
            return f"[bold {RED}]✗ authentication failed[/] [{GREY}]{_short(err, 100)}[/][{AMBER}]{hint}[/]"
        return f"[{AMBER}]{escape(failure.lower().replace('_', ' '))}[/] [{GREY}]{_short(d.get('error') or d.get('action'), 120)}[/]"
    if t == EventType.LOOP_DETECTED:
        return f"[{AMBER}]↻ loop detected[/] [{GREY}]{_short(d.get('reason'), 120)} → {d.get('action')}[/]"
    if t == EventType.BUDGET_WARNING:
        return f"[{AMBER}]$ budget warning[/] [{GREY}]${d.get('total_usd')} of ${d.get('limit')}[/]"
    if t == EventType.PROVIDER_RETRY:
        return (
            f"[{AMBER}]⟳ retrying {d.get('model')} in {d.get('delay_seconds')}s[/] "
            f"[{GREY}](attempt {d.get('attempt')}: {_short(d.get('error'), 100)})[/]"
        )
    if t == EventType.PROVIDER_FALLBACK:
        return f"[{AMBER}]⤳ {d.get('from_model')} failed — trying the next provider[/] [{GREY}]{_short(d.get('error'), 100)}[/]"
    if t == EventType.STEERED:
        return f"[bold {MINT}]↳ steering applied[/]"
    if t == EventType.PATHS_TRANSLATED:
        return f"[{GREY}]↳ translated {d.get('count')} Windows path(s) to WSL paths[/]"
    if t == EventType.REMOTE_MESSAGE:
        return f"[bold {MINT}]📱 Telegram ❯[/] [{MINT}]{escape(str(d.get('text')))}[/]"
    if t == EventType.REMOTE_CHANNEL_STARTED and d.get("channel") == "telegram-bridge":
        return f"[bold {NEON}]📱 Telegram remote control on[/] [{GREY}]chat {d.get('chat_id')}[/]"
    if t == EventType.PLAN_GATE_REQUESTED:
        return f"[bold {AMBER}]⏸ plan gate: waiting for your go-ahead before the first change[/]"
    if t == EventType.PLAN_APPROVED:
        return f"[bold {NEON}]▶ plan approved[/] [{GREY}]via {d.get('via')}[/]"
    if t == EventType.PLAN_REJECTED:
        return f"[bold {RED}]■ plan rejected[/] [{GREY}]{_short(d.get('reason'), 120)}[/]"
    if t == EventType.SESSION_BRANCHED:
        return f"[{GREY}]⑂ branched → {d.get('child')}[/]"
    if t == EventType.FILES_ATTACHED:
        return f"[{GREY}]📎 attached {', '.join(d.get('files') or [])}[/]"
    if t == EventType.VISION_AUTOSWITCH:
        return (
            f"[bold {MINT}]👁 using {d.get('to_model')} for this prompt[/] "
            f"[{GREY}](cheapest model that can see images; back to {d.get('from_model')} after)[/]"
        )
    if t == EventType.MEMORY_UPDATED:
        added = d.get("added") or []
        shown = "; ".join(escape(str(a)) for a in added[:3]) + (" …" if len(added) > 3 else "")
        return f"[{MINT}]🧠 remembered for this project:[/] [{GREY}]{shown}[/]"
    if t == EventType.IMAGES_ATTACHED:
        return f"[{GREY}]🖼 attached {', '.join(d.get('images') or [])}[/]"
    return None


def run_footer(result) -> str:
    """One quiet line under an answer: outcome, calls, time, cost, changed files, validation."""
    glyph = {"COMPLETED": f"[bold {NEON}]✓ done[/]", "CANCELED": f"[{AMBER}]■ canceled[/]"}.get(
        result.status, f"[bold {RED}]✗ stopped[/]"
    )
    parts = [glyph]
    if result.status != "COMPLETED" and result.stop_reason:
        parts.append(f"[{AMBER}]{escape(str(result.stop_reason))}[/]")
    parts.append(
        f"[{GREY}]{result.model_calls} calls · {result.elapsed_s:.0f}s · ${result.cost_usd:.3f}[/]"
    )
    if result.changed_files:
        files = result.changed_files
        shown = ", ".join(files[:4]) + (f" +{len(files) - 4}" if len(files) > 4 else "")
        parts.append(f"[{NEON_DIM}]changed[/] [{GREY}]{escape(shown)}[/]")
        if result.validation_runs:
            ok = all(r.get("ok") for r in result.validation_runs)
            parts.append(f"[{NEON if ok else RED}]validated {'✓' if ok else '✗'}[/]")
        else:
            parts.append(f"[{AMBER}]not validated[/]")
        ver = getattr(result, "verification", None) or {}
        v = ver.get("verdict")
        if v == "pass":
            parts.append(f"[{NEON}]verified ✓[/]")
        elif v in {"fix", "fail"}:
            n = len(ver.get("findings") or [])
            parts.append(f"[{AMBER if v == 'fix' else RED}]verifier: {v} ({n})[/]")
    return "  ".join(parts)
