"""Checkpoints and undo (spec §28). Snapshots live in ``.trendlab/checkpoints/<id>/``; undo
refuses to overwrite files the user changed since the checkpoint."""

from __future__ import annotations

import hashlib
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trendlab.sessions.store import SessionStore


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class CheckpointManager:
    def __init__(self, project_root: Path, store: SessionStore, session_id: str) -> None:
        self.root = project_root
        self.store = store
        self.session_id = session_id
        self.dir = project_root / ".trendlab" / "checkpoints"

    def create(
        self,
        files: list[str],
        *,
        label: str | None = None,
        git_head: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        cid = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        snap = self.dir / cid
        snap.mkdir(parents=True, exist_ok=True)
        entries = []
        for rel in sorted(set(files)):
            src = self.root / rel
            pre = _sha(src)
            if pre is not None:
                dest = snap / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            entries.append({"path": rel, "pre_sha256": pre, "post_sha256": None})
        row = {
            "id": cid,
            "session_id": self.session_id,
            "created_at": datetime.now(UTC).isoformat(),
            "label": label,
            "git_head": git_head,
            "task_id": task_id,
            "files": entries,
        }
        self.store.insert_checkpoint(row)
        return row

    def extend(self, checkpoint_id: str, files: list[str]) -> None:
        """Snapshot additional files into an existing (unsealed) checkpoint."""
        cps = {c["id"]: c for c in self.list()}
        cp = cps.get(checkpoint_id)
        if cp is None:
            return
        known = {e["path"] for e in cp["files"]}
        snap = self.dir / checkpoint_id
        for rel in sorted(set(files) - known):
            src = self.root / rel
            pre = _sha(src)
            if pre is not None:
                dest = snap / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            cp["files"].append({"path": rel, "pre_sha256": pre, "post_sha256": None})
        self.store.delete_checkpoint(checkpoint_id)
        self.store.insert_checkpoint(cp)

    def seal(self, checkpoint_id: str) -> None:
        """Record post-edit hashes so undo can detect later external modifications."""
        for cp in self.store.checkpoints(self.session_id):
            if cp["id"] == checkpoint_id:
                for e in cp["files"]:
                    e["post_sha256"] = _sha(self.root / e["path"])
                self.store.delete_checkpoint(checkpoint_id)
                self.store.insert_checkpoint(cp)
                return

    def list(self) -> list[dict[str, Any]]:
        return self.store.checkpoints(self.session_id)

    def undo(self, checkpoint_id: str | None = None, *, force: bool = False) -> dict[str, Any]:
        cps = self.list()
        if not cps:
            return {"ok": False, "error": "no checkpoints"}
        cp = next((c for c in cps if c["id"] == checkpoint_id), None) if checkpoint_id else cps[-1]
        if cp is None:
            return {"ok": False, "error": f"unknown checkpoint {checkpoint_id}"}
        conflicts = []
        for e in cp["files"]:
            now = _sha(self.root / e["path"])
            if e.get("post_sha256") is not None and now != e["post_sha256"]:
                conflicts.append(e["path"])
        if conflicts and not force:
            return {
                "ok": False,
                "error": "files changed since the checkpoint; use force to overwrite",
                "conflicts": conflicts,
                "checkpoint": cp["id"],
            }
        restored, removed = [], []
        for e in cp["files"]:
            target = self.root / e["path"]
            snap = self.dir / cp["id"] / e["path"]
            if e["pre_sha256"] is None:
                if target.exists():
                    target.unlink()
                    removed.append(e["path"])
            elif snap.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap, target)
                restored.append(e["path"])
        self.store.delete_checkpoint(cp["id"])
        shutil.rmtree(self.dir / cp["id"], ignore_errors=True)
        return {"ok": True, "checkpoint": cp["id"], "restored": restored, "removed": removed}


def undo_step(manager: CheckpointManager, step_id: str) -> dict[str, Any]:
    """Undo back to before plan step ``step_id``: that step's checkpoint and every checkpoint
    taken after it are undone, newest first (later steps built on this one)."""
    cps = sorted(manager.list(), key=lambda c: c["created_at"])
    idx = next(
        (i for i, c in enumerate(cps) if str(c.get("label") or "").startswith(f"step {step_id}:")),
        None,
    )
    if idx is None:
        return {"ok": False, "error": f"no checkpoint for step {step_id}"}
    restored: list[str] = []
    removed: list[str] = []
    for cp in reversed(cps[idx:]):
        res = manager.undo(cp["id"], force=True)
        if res.get("ok"):
            restored += res["restored"]
            removed += res["removed"]
    return {
        "ok": True,
        "step": step_id,
        "restored": sorted(set(restored)),
        "removed": sorted(set(removed) - set(restored)),
    }
