"""Real-world failure drills (spec §92.4) against the live DeepSeek API.

Runs in a throwaway TrendLab home (secrets copied, Telegram off) so the interactive session and
its database are untouched:

  A. provider down → retries with backoff → fallback to the next model → task completes
  B. process killed while an approval is pending → next start cancels it, nothing executes

    .venv/bin/python scripts/failure_drills.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRENDLAB = str(ROOT / ".venv" / "bin" / "trendlab")
REAL_HOME = Path(os.environ.get("TRENDLAB_REAL_HOME", Path.home() / ".trendlab"))


def make_home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / "secrets").mkdir(parents=True)
    for f in (REAL_HOME / "secrets").glob("*_API_KEY"):
        shutil.copy(f, home / "secrets" / f.name)
        os.chmod(home / "secrets" / f.name, 0o600)
    (home / "config.toml").write_text(
        """
[defaults]
model = "deepseek:deepseek-flash"
permission_mode = "auto"

[retry]
max_attempts = 3
base_delay_seconds = 0.2
max_delay_seconds = 0.5

[providers.deepseek]
type = "openai_compatible"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"

[providers.dead]
type = "openai_compatible"
base_url = "http://dead.invalid/v1"
api_key_env = "DEEPSEEK_API_KEY"

[fallback]
"dead:m" = ["deepseek:deepseek-flash"]

[models."deepseek:deepseek-flash"]
context_window = 1000000

[pricing."deepseek:deepseek-flash"]
input_per_million = 0.30
output_per_million = 1.20

[telegram_bridge]
enabled = false

[memory]
learn = false
"""
    )
    return home


def env_for(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {"TRENDLAB_HOME": str(home), "TRENDLAB_TELEGRAM": "off", "TRENDLAB_NO_UPDATE_CHECK": "1"}
    )
    return env


def events(home: Path) -> list[dict]:
    path = home / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def drill_a(home: Path, project: Path) -> dict:
    t0 = time.time()
    proc = subprocess.run(
        [
            TRENDLAB,
            "-m",
            "dead:m",
            "-C",
            str(project),
            "-p",
            "Reply with exactly the single word READY.",
            "--output",
            "json",
        ],
        env=env_for(home),
        capture_output=True,
        text=True,
        timeout=180,
    )
    elapsed = round(time.time() - t0, 1)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = {"status": "NO_JSON", "stderr": proc.stderr[-400:]}
    evs = events(home)
    retries = [e for e in evs if e["event"] == "provider.retry"]
    fallbacks = [e for e in evs if e["event"] == "provider.fallback"]
    completed = [e for e in evs if e["event"] == "model.call_completed"]
    return {
        "status": result.get("status"),
        "text": (result.get("text") or "")[:40],
        "elapsed_s": elapsed,
        "retries_on_dead": len(retries),
        "fallback_events": [(e.get("from_model"), e.get("error", "")[:60]) for e in fallbacks],
        "model_that_answered": completed[-1].get("model") if completed else None,
        "cost_usd": result.get("cost_usd"),
    }


def drill_b(home: Path, project: Path) -> dict:
    db = home / "sessions.db"
    target = project / "hello_drill.txt"
    child = subprocess.Popen(
        [
            TRENDLAB,
            "--safe",
            "-C",
            str(project),
            "-p",
            "Create a file named hello_drill.txt containing the single word hi. "
            "Do not ask questions.",
            "--output",
            "json",
        ],
        env=env_for(home),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pending = None
    for _ in range(600):  # up to 60 s for the model to propose the write
        time.sleep(0.1)
        if db.exists():
            c = sqlite3.connect(db)
            row = c.execute(
                "SELECT id, status, tool, summary FROM approvals WHERE status='pending' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            c.close()
            if row:
                pending = row
                break
    if pending is None:
        child.kill()
        return {"error": "no pending approval appeared within 60 s"}
    time.sleep(0.5)
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    existed_after_kill = target.exists()
    # Next start recovers: stale approvals are cancelled, never executed.
    subprocess.run(
        [TRENDLAB, "-C", str(project), "-p", "Reply with the single word OK.", "--output", "json"],
        env=env_for(home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    c = sqlite3.connect(db)
    row = c.execute(
        "SELECT status, resolution_reason FROM approvals WHERE id=?", (pending[0],)
    ).fetchone()
    c.close()
    cancels = [
        e
        for e in events(home)
        if e["event"] == "approval.canceled" and e.get("reason") == "process_restart"
    ]
    return {
        "pending_before_kill": {"tool": pending[2], "summary": pending[3]},
        "file_created_by_killed_process": existed_after_kill,
        "status_after_restart": row[0] if row else None,
        "resolution_reason": row[1] if row else None,
        "restart_cancel_events": len(cancels),
        "file_exists_after_restart": target.exists(),
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="trendlab-drills-"))
    home = make_home(tmp)
    project = tmp / "proj"
    project.mkdir()
    (project / "README.md").write_text("# drill project\n")
    subprocess.run(["git", "init", "-q", str(project)], check=False)
    report = {
        "A_provider_down_fallback": drill_a(home, project),
        "B_restart_mid_approval": drill_b(home, project),
    }
    print(json.dumps(report, indent=2))
    ok_a = (
        report["A_provider_down_fallback"].get("status") == "COMPLETED"
        and report["A_provider_down_fallback"]["retries_on_dead"] >= 1
    )
    b = report["B_restart_mid_approval"]
    ok_b = b.get("status_after_restart") == "canceled" and not b.get("file_exists_after_restart")
    print("\nDRILL A:", "PASS" if ok_a else "FAIL", "| DRILL B:", "PASS" if ok_b else "FAIL")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok_a and ok_b else 1


if __name__ == "__main__":
    sys.exit(main())
