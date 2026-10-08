"""Single pane of glass (uplift U6): a self-contained HTML dashboard and a flat export.

`trendlab dashboard` renders sessions, spend by day, outcomes, failure reasons, guards, tool
reliability, model mix, the canary history and the inbox into one static HTML file (no
scripts, no network). `trendlab export` writes the same data as CSV / JSONL for a spreadsheet
or a warehouse.
"""

from __future__ import annotations

import csv
import html
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def collect(store, days: int = 14, canary_path: Path | None = None, inbox=None) -> dict[str, Any]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    st = store.stats(days)
    by_day: dict[str, float] = defaultdict(float)
    calls_by_day: Counter = Counter()
    with store._lock:  # noqa: SLF001 — read-only aggregate queries
        for r in store._conn.execute(  # noqa: SLF001
            "SELECT substr(ts,1,10) AS d, SUM(cost_usd) AS c, COUNT(*) AS n FROM model_calls "
            "WHERE ts >= ? GROUP BY d ORDER BY d",
            (since,),
        ).fetchall():
            by_day[r["d"]] = round(r["c"] or 0.0, 4)
            calls_by_day[r["d"]] = r["n"]
        tool_rows = store._conn.execute(  # noqa: SLF001
            "SELECT data FROM events WHERE ts >= ? AND type = 'tool.completed'", (since,)
        ).fetchall()
        harness_rows = store._conn.execute(  # noqa: SLF001
            "SELECT COALESCE(label, 'trendlab') AS h, COUNT(*) AS n FROM sessions "
            "WHERE updated_at >= ? GROUP BY h",
            (since,),
        ).fetchall()
    tools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in tool_rows:
        d = json.loads(r["data"] or "{}")
        t = str(d.get("tool"))
        tools[t][0] += 1
        err = str(d.get("error") or "")
        if t in {"shell", "run_tests", "background"}:
            # a failing test or non-zero exit is a result; only tool errors count
            failed = err.startswith(("tool error", "command timed out", "timed out"))
        else:
            failed = not d.get("ok", True)
        tools[t][1] += 0 if failed else 1
    canary = []
    if canary_path and canary_path.is_file():
        canary = [
            json.loads(ln)
            for ln in canary_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ][-14:]
    inbox_counts = inbox.counts() if inbox is not None else {}
    inbox_top = inbox.list(status="open", limit=5) if inbox is not None else []
    return {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "days": days,
        "stats": st,
        "cost_by_day": dict(by_day),
        "calls_by_day": dict(calls_by_day),
        "tools": {
            t: {"calls": v[0], "success": round(v[1] / v[0], 3) if v[0] else 1.0}
            for t, v in sorted(tools.items(), key=lambda kv: -kv[1][0])
        },
        "harnesses": {r["h"]: r["n"] for r in harness_rows},
        "canary": canary,
        "inbox": {"counts": inbox_counts, "top": inbox_top},
    }


_CSS = """
:root{--bg:#000;--panel:#0b0f0b;--line:#123312;--neon:#39ff14;--dim:#7fbf6f;--text:#d8f5d0;--amber:#ffd21f;--red:#ff4d4d}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f6fff4;--panel:#fff;--line:#cfe8c8;--neon:#127a00;--dim:#3c6b33;--text:#0d1f0a;--amber:#a06b00;--red:#b00020}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:16px}
h1{color:var(--neon);font-size:20px;margin:0 0 4px}h2{color:var(--neon);font-size:14px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.08em}
.sub{color:var(--dim);margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:12px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}.tile b{display:block;color:var(--neon);font-size:20px}
table{border-collapse:collapse;width:100%}td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--dim);font-weight:normal}
.bar{display:flex;align-items:flex-end;gap:3px;height:90px}.bar div{background:var(--neon);flex:1;min-width:6px;border-radius:2px 2px 0 0}
.warn{color:var(--amber)}.bad{color:var(--red)}.ok{color:var(--neon)}
"""


def _table(rows: list[tuple], head: tuple) -> str:
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in head)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"<table><tr>{h}</tr>{body}</table>" if rows else "<div class='sub'>nothing yet</div>"


def render_html(data: dict[str, Any]) -> str:
    st = data["stats"]
    runs = st.get("runs", {})
    done, failed = runs.get("completed", 0), runs.get("failed", 0)
    rate = f"{done / (done + failed):.0%}" if done + failed else "–"
    tiles = [
        ("sessions", st["sessions"]),
        ("projects", st["projects"]),
        ("model calls", st["model_calls"]),
        ("spend", f"${st['cost_usd']:.2f}"),
        ("run success", rate),
        ("breaker trips", st.get("breakers_opened", 0)),
        (
            "validated (changes)",
            f"{st['online_quality']['validated_rate']:.0%}"
            if (st.get("online_quality") or {}).get("validated_rate") is not None
            else "–",
        ),
        (
            "unsupported claims",
            f"{(st.get('online_quality') or {}).get('unsupported_claim_rate', 0):.0%}",
        ),
    ]
    tile_html = "".join(
        f"<div class='tile'>{html.escape(k)}<b>{html.escape(str(v))}</b></div>" for k, v in tiles
    )
    costs = data["cost_by_day"]
    peak = max(costs.values()) if costs else 0
    bars = "".join(
        f"<div title='{html.escape(d)} ${c:.3f}' style='height:{max(3, int(85 * c / peak)) if peak else 3}px'></div>"
        for d, c in costs.items()
    )
    tools = _table(
        [(t, v["calls"], f"{v['success']:.0%}") for t, v in list(data["tools"].items())[:15]],
        ("tool", "calls", "success"),
    )
    canary = _table(
        [
            (
                str(c.get("at", ""))[:16],
                c.get("passes"),
                c.get("passes_ci95"),
                f"${c.get('cost', 0):.3f}",
                "; ".join(c.get("cause_hints") or [])[:60],
            )
            for c in reversed(data["canary"])
        ],
        ("night", "passes", "95% CI", "cost", "notes"),
    )
    inbox = _table(
        [
            (i["severity"], i["occurrences"], i["title"][:70], i["source"])
            for i in data["inbox"]["top"]
        ],
        ("sev", "×", "title", "source"),
    )
    lists = []
    for title, key in (
        ("Failure reasons", "failure_reasons"),
        ("Guards fired", "guards"),
        ("Skipped tools", "skipped_tools"),
        ("Verifier verdicts", "verifier_verdicts"),
        ("Models", "models"),
        ("Routes", "routes"),
    ):
        lists.append(
            f"<div class='card'><h2>{title}</h2>"
            + _table(list(st.get(key, {}).items()), ("", "count"))
            + "</div>"
        )
    harness = _table(list(data["harnesses"].items()), ("harness", "sessions"))
    counts = ", ".join(f"{v} {k}" for k, v in data["inbox"]["counts"].items()) or "empty"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TrendLab Dashboard</title>
<style>{_CSS}</style></head><body>
<h1>TRENDLAB · dashboard</h1><div class="sub">last {data["days"]} days · generated {html.escape(data["generated"])}</div>
<div class="tiles">{tile_html}</div>
<div class="grid">
<div class="card"><h2>Spend by day</h2><div class="bar">{bars}</div></div>
<div class="card"><h2>Tool reliability</h2>{tools}</div>
<div class="card"><h2>Nightly canary</h2>{canary}</div>
<div class="card"><h2>Inbox · {html.escape(counts)}</h2>{inbox}</div>
<div class="card"><h2>Sessions by harness</h2>{harness}</div>
{"".join(lists)}
</div></body></html>
"""


def export(
    store, out_dir: Path, *, days: int | None = None, messages: bool = False
) -> dict[str, int]:
    """Flat files for a spreadsheet or warehouse: sessions.csv, model_calls.csv, events.jsonl
    (and messages.jsonl when asked)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days else ""
    counts = {}
    with store._lock:  # noqa: SLF001
        conn = store._conn  # noqa: SLF001
        for table, where in (("sessions", "updated_at"), ("model_calls", "ts")):
            rows = conn.execute(f"SELECT * FROM {table} WHERE {where} >= ?", (since,)).fetchall()
            with (out_dir / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if rows:
                    w.writerow(rows[0].keys())
                    w.writerows([tuple(r) for r in rows])
            counts[table] = len(rows)
        tables = [("events", "data")] + ([("messages", "payload")] if messages else [])
        for table, col in tables:
            n = 0
            with (out_dir / f"{table}.jsonl").open("w", encoding="utf-8") as fh:
                for r in conn.execute(f"SELECT * FROM {table} WHERE ts >= ?", (since,)):
                    d = dict(r)
                    d[col] = json.loads(d[col]) if d.get(col) else None
                    fh.write(json.dumps(d, default=str) + "\n")
                    n += 1
            counts[table] = n
    return counts
