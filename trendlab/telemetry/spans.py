"""Spans (uplift U6): timed, nested units of work stamped onto every event.

A span is opened with ``bus.span("model_call", model=...)``; every event emitted inside it
carries ``span_id`` and ``parent_id``, and the span itself emits ``span.started`` /
``span.ended {duration_ms}``. Spans nest through a context variable, so concurrent tool calls
inside one batch each get their own child span. Stored events can be rebuilt into a trace
tree (``trace_tree``) and exported in an OpenTelemetry-style JSON shape (``to_otel``).
"""

from __future__ import annotations

import contextvars
import secrets
import time
from contextlib import contextmanager
from typing import Any

_current: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar(
    "trendlab_span", default=None
)


def current_span() -> tuple[str, str | None] | None:
    return _current.get()


def new_span_id() -> str:
    return secrets.token_hex(6)


@contextmanager
def span_scope(span_id: str, parent_id: str | None):
    token = _current.set((span_id, parent_id))
    try:
        yield
    finally:
        _current.reset(token)


def trace_tree(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild nested spans from stored event records (``span.started`` / ``span.ended`` plus
    the events stamped with ``span_id``). Returns the root spans, each with ``children``,
    ``duration_ms``, ``events`` (count) and the span's attributes."""
    spans: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in records:
        t = r.get("event") or r.get("type")
        if t == "span.started":
            sid = r.get("span_id")
            if not sid:
                continue
            spans[sid] = {
                "span_id": sid,
                "parent_id": r.get("parent_id"),
                "name": r.get("name", "?"),
                "start": r.get("ts"),
                "end": None,
                "duration_ms": None,
                "attrs": {
                    k: v
                    for k, v in r.items()
                    if k
                    not in {"event", "type", "ts", "session_id", "span_id", "parent_id", "name"}
                },
                "events": 0,
                "children": [],
            }
            order.append(sid)
        elif t == "span.ended":
            s = spans.get(r.get("span_id") or "")
            if s is not None:
                s["end"] = r.get("ts")
                s["duration_ms"] = r.get("duration_ms")
                s["status"] = r.get("status", "ok")
        else:
            s = spans.get(r.get("span_id") or "")
            if s is not None:
                s["events"] += 1
    roots: list[dict[str, Any]] = []
    for sid in order:
        s = spans[sid]
        parent = spans.get(s["parent_id"] or "")
        (parent["children"] if parent else roots).append(s)
    return roots


def render_tree(roots: list[dict[str, Any]], *, depth: int = 0, max_depth: int = 6) -> list[str]:
    lines: list[str] = []
    for s in roots:
        dur = f"{s['duration_ms']} ms" if s.get("duration_ms") is not None else "…"
        attrs = " ".join(
            f"{k}={str(v)[:40]}"
            for k, v in s["attrs"].items()
            if k in {"model", "tool", "step", "role", "n", "status"}
        )
        lines.append(
            "  " * depth
            + f"{s['name']}  {dur}"
            + (f"  [{attrs}]" if attrs else "")
            + (f"  ·{s['events']} ev" if s["events"] else "")
        )
        if depth < max_depth:
            lines.extend(render_tree(s["children"], depth=depth + 1, max_depth=max_depth))
    return lines


def _flatten(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for s in roots:
        out.append(s)
        out.extend(_flatten(s["children"]))
    return out


def to_otel(session_id: str, roots: list[dict[str, Any]]) -> dict[str, Any]:
    """OpenTelemetry-style JSON (resourceSpans → scopeSpans → spans) so traces can be read by
    any collector or viewer; a cross-harness normalisation target."""
    from datetime import datetime

    def ns(ts: str | None) -> int:
        if not ts:
            return 0
        try:
            return int(datetime.fromisoformat(ts).timestamp() * 1_000_000_000)
        except ValueError:
            return 0

    trace_id = (session_id + "0" * 32)[:32]
    spans = []
    for s in _flatten(roots):
        spans.append(
            {
                "traceId": trace_id,
                "spanId": s["span_id"].ljust(16, "0")[:16],
                "parentSpanId": (s["parent_id"] or "").ljust(16, "0")[:16]
                if s["parent_id"]
                else "",
                "name": s["name"],
                "startTimeUnixNano": ns(s["start"]),
                "endTimeUnixNano": ns(s["end"]),
                "status": {"code": 2 if s.get("status") == "error" else 1},
                "attributes": [
                    {"key": k, "value": {"stringValue": str(v)[:200]}}
                    for k, v in s["attrs"].items()
                ],
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "trendlab-cli"}}
                    ]
                },
                "scopeSpans": [{"scope": {"name": "trendlab"}, "spans": spans}],
            }
        ]
    }


class SpanClock:
    """Helper the bus uses to time spans without importing time everywhere."""

    @staticmethod
    def now() -> float:
        return time.monotonic()
