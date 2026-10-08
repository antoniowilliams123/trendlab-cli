"""``trendlab engine``: the local daemon (cheap-model spec §7.1).

Owns the scheduler (watch / sleep / meta), the inbox store and a Unix socket that clients
query for status and the latest digest. Sessions still run in the client process.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trendlab.config.loader import load_config, trendlab_home
from trendlab.engine.inbox import Inbox, digest
from trendlab.engine.notify import send_telegram


def engine_dir() -> Path:
    d = trendlab_home() / "engine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox_path() -> Path:
    return engine_dir() / "inbox.db"


def socket_path() -> Path:
    return engine_dir() / "engine.sock"


def pid_path() -> Path:
    return engine_dir() / "engine.pid"


def state_path() -> Path:
    return engine_dir() / "state.json"


def read_pid() -> int | None:
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


class Engine:
    def __init__(self, *, projects: list[Path], config=None, tick_seconds: float = 30.0) -> None:
        self.config = config or load_config()
        self.projects = [p.resolve() for p in projects]
        self.tick = tick_seconds
        self.inbox = Inbox(inbox_path())
        self.state: dict[str, Any] = self._load_state()
        self._stop = asyncio.Event()
        self.started = time.time()
        self.runs: list[dict[str, Any]] = []

    # -- persistence -------------------------------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(state_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        state_path().write_text(json.dumps(self.state, indent=1))

    # -- schedule ----------------------------------------------------------------------------
    def due(self, job: str, now: datetime) -> bool:
        ecfg = self.config.engine
        last = self.state.get(f"last_{job}")
        if job == "watch":
            return not last or (now.timestamp() - float(last)) >= ecfg.watch_minutes * 60
        warm = (time.time() - self.started) < 120  # model-backed jobs wait out the first tick
        if job == "sleep":
            hour, minute = (int(x) for x in ecfg.sleep_at.split(":")[:2])
            stamp = now.strftime("%Y-%m-%d")
            after = (now.hour, now.minute) >= (hour, minute)
            recent = last is not None and (now.timestamp() - float(last)) < 20 * 3600
            return not warm and after and not recent and self.state.get("sleep_day") != stamp
        if job == "meta":
            return not warm and (
                not last or (now.timestamp() - float(last)) >= ecfg.meta_days * 86400
            )
        if job == "digest":
            return not last or (now.timestamp() - float(last)) >= ecfg.digest_minutes * 60
        if job == "prune":
            stamp = now.strftime("%Y-%m-%d")
            return not warm and now.hour >= 4 and self.state.get("prune_day") != stamp
        if job == "review":
            if not ecfg.review_commits:
                return False
            return not warm and (
                not last or (now.timestamp() - float(last)) >= ecfg.review_minutes * 60
            )
        if job == "health":
            stamp = now.strftime("%Y-%m-%d")
            return not warm and now.hour >= 5 and self.state.get("health_day") != stamp
        if job == "budget":
            econ = self.config.economics
            if not (econ.monthly_budget_usd or econ.project_budgets):
                return False
            return not last or (now.timestamp() - float(last)) >= 3600
        if job == "canary":
            if not ecfg.canary:
                return False
            hour, minute = (int(x) for x in ecfg.canary_at.split(":")[:2])
            stamp = now.strftime("%Y-%m-%d")
            after = (now.hour, now.minute) >= (hour, minute)
            recent = last is not None and (now.timestamp() - float(last)) < 20 * 3600
            return not warm and after and not recent and self.state.get("canary_day") != stamp
        return False

    @staticmethod
    def now() -> datetime:
        """Local wall-clock time; the schedule (sleep_at, day stamps) is in the owner's zone."""
        return datetime.now(UTC).astimezone()

    async def run_job(self, job: str) -> dict[str, Any]:
        now = self.now()
        result: dict[str, Any] = {"job": job, "at": now.isoformat(timespec="seconds")}
        try:
            if job == "watch":
                from trendlab.engine.watch import watch_projects

                result["issues"] = await watch_projects(self.inbox, self.projects, self.config)
            elif job == "sleep":
                from trendlab.engine.jobs import sleep_projects

                # stamp first: a failing pass must not retry on every tick
                self.state["sleep_day"] = now.strftime("%Y-%m-%d")
                result["sleep"] = await sleep_projects(self.projects, self.config)
            elif job == "meta":
                from trendlab.engine.jobs import meta_scan

                result["meta"] = await meta_scan(self.inbox, self.config)
            elif job == "digest":
                result["sent"] = await self.send_digest()
            elif job == "prune":
                from trendlab.sessions.store import SessionStore

                self.state["prune_day"] = now.strftime("%Y-%m-%d")
                store = SessionStore(trendlab_home() / "sessions.db")
                try:
                    result["pruned"] = store.prune(
                        older_than_days=self.config.sessions.retention_days,
                        keep_latest=self.config.sessions.keep_latest,
                    )
                finally:
                    store.close()
            elif job == "review":
                from trendlab.engine.jobs import review_commits

                result["review"] = await review_commits(
                    self.inbox, self.config, self.state, self.projects
                )
            elif job == "health":
                from trendlab.engine.health import health_projects

                self.state["health_day"] = now.strftime("%Y-%m-%d")
                result["health"] = await health_projects(self.inbox, self.projects)
            elif job == "budget":
                from trendlab.engine.jobs import budget_check

                result["budget"] = await budget_check(self.inbox, self.config, self.state)
            elif job == "canary":
                from trendlab.engine.jobs import canary_run

                self.state["canary_day"] = now.strftime("%Y-%m-%d")
                result["canary"] = await canary_run(self.inbox, self.config, self.state)
        except Exception as exc:  # noqa: BLE001 — one job must never kill the engine
            result["error"] = f"{exc.__class__.__name__}: {exc}"[:300]
        self.state[f"last_{job}"] = time.time()
        self._save_state()
        self.runs.append(result)
        self.runs = self.runs[-50:]
        return result

    async def send_digest(self) -> bool:
        """One message per cycle instead of per event (§7.2); skipped when nothing is open."""
        counts = self.inbox.counts()
        if not counts.get("open"):
            return False
        text = digest(self.inbox.list(status="open", limit=3), counts)
        if self.state.get("last_digest_text") == text:
            return False
        ok = await send_telegram(self.config, text)
        if ok:
            self.state["last_digest_text"] = text
        return ok

    # -- status + socket ---------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started),
            "projects": [str(p) for p in self.projects],
            "inbox": self.inbox.counts(),
            "last": {
                k: v
                for k, v in self.state.items()
                if k.startswith("last_") and k != "last_digest_text"
            },
            "recent_runs": self.runs[-5:],
        }

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            req = json.loads(line.decode() or "{}")
            cmd = req.get("cmd", "status")
            if cmd == "status":
                resp = self.status()
            elif cmd == "digest":
                resp = {
                    "text": digest(self.inbox.list(status="open", limit=3), self.inbox.counts())
                }
            elif cmd == "run" and req.get("job") in {
                "watch",
                "sleep",
                "meta",
                "digest",
                "canary",
                "prune",
                "budget",
                "health",
                "review",
            }:
                resp = await self.run_job(req["job"])
            elif cmd == "stop":
                resp = {"stopping": True}
                self._stop.set()
            else:
                resp = {"error": f"unknown command {cmd!r}"}
        except Exception as exc:  # noqa: BLE001
            resp = {"error": str(exc)[:200]}
        writer.write((json.dumps(resp, default=str) + "\n").encode())
        await writer.drain()
        writer.close()

    async def run(self) -> None:
        pid_path().write_text(str(os.getpid()))
        sock = socket_path()
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self._serve, path=str(sock))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            while not self._stop.is_set():
                now = self.now()
                for job in (
                    "watch",
                    "sleep",
                    "meta",
                    "digest",
                    "canary",
                    "prune",
                    "budget",
                    "health",
                    "review",
                ):
                    if self.due(job, now):
                        await self.run_job(job)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
                except TimeoutError:
                    pass
        finally:
            server.close()
            await server.wait_closed()
            for p in (sock, pid_path()):
                try:
                    p.unlink()
                except OSError:
                    pass
            self.inbox.close()


async def client(cmd: str, **kw: Any) -> dict[str, Any] | None:
    """Talk to a running engine; None when it is not running."""
    sock = socket_path()
    if not sock.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(sock))
    except OSError:
        return None
    writer.write((json.dumps({"cmd": cmd, **kw}) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=600)
    writer.close()
    return json.loads(line.decode() or "{}")
