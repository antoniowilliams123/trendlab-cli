"""File tools with project-boundary enforcement, ignore rules and conflict protection."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import re
import secrets
import shlex
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import NoReturn

from pydantic import BaseModel, Field

from trendlab.context.ignore import IgnoreRules
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.security.sandbox import SYSTEM_RO
from trendlab.tools.base import PathOutsideProjectError, Tool, ToolContext, ToolResult
from trendlab.ui.diff_view import diff_stats, unified_diff

_SENSITIVE = re.compile(
    r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|id_ecdsa|.*\.pem|.*\.key|credentials(\.json)?|"
    r"\.netrc|\.npmrc|\.pypirc)$"
)
MAX_FILE_BYTES = 500_000


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_binary(sample: bytes) -> bool:
    return b"\x00" in sample[:8000]


# Searching a big tree (a home folder with data archives) must never freeze the UI or run for
# minutes: the walk runs in a worker thread, checks a deadline and a stop flag as it goes, skips
# big and binary files the way ripgrep does, and says when it stopped early.
SEARCH_DEADLINE_S = 20.0
MAX_SEARCH_BYTES = 2_000_000
_SKIP_EXT = frozenset(
    ".parquet .gz .zip .bz2 .xz .zst .7z .tar .pkl .pickle .npy .npz .h5 .hdf5 .feather "
    ".arrow .db .sqlite .dbn .png .jpg .jpeg .gif .webp .pdf .mp4 .mov .mp3 .wav .so .dll "
    ".exe .bin .whl .pyc .class .jar .iso .img".split()
)


def _iter_files(root: Path, project: Path, rules: IgnoreRules, stop: threading.Event):
    """Files under ``root`` (depth first, sorted), pruning ignored directories, until stopped."""
    stack = [root]
    while stack and not stop.is_set():
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name, reverse=True)
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            continue
        for entry in entries:
            if stop.is_set():
                return
            try:
                rel = entry.relative_to(project).as_posix()
            except ValueError:
                rel = entry.name
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if not rules.ignored(rel, is_dir=True):
                    stack.append(entry)
            elif not rules.ignored(rel):
                yield entry, rel


async def _in_thread(fn, stop: threading.Event):
    """Run ``fn`` in a worker thread; cancelling the run (Esc) sets ``stop`` so it ends soon."""
    try:
        return await asyncio.to_thread(fn)
    except asyncio.CancelledError:
        stop.set()
        raise


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob → regex over project-relative paths: ** spans folders, * and ? stay in one."""
    pat = pattern.strip().lstrip("./")
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _perm_or_outside(
    tool: str,
    ctx: ToolContext,
    raw: str,
    category: OperationCategory,
    summary: str,
    args: dict,
    affected: list[str] | None = None,
    preview: str | None = None,
    explanation: str = "",
) -> PermissionRequest:
    try:
        resolved = ctx.resolve(raw)
        rel = str(resolved.relative_to(ctx.project_root.resolve())).replace(os.sep, "/")
        if _SENSITIVE.search(rel):
            category = (
                OperationCategory.OUTSIDE_PROJECT
            )  # credential files are treated as an escape
            summary = f"{summary} (sensitive file)"
    except PathOutsideProjectError:
        category = OperationCategory.OUTSIDE_PROJECT
        rel = raw
    return PermissionRequest(
        tool=tool,
        category=category,
        summary=summary,
        command=None,
        cwd=str(ctx.project_root),
        affected_files=affected if affected is not None else [rel],
        args=args,
        task_id=ctx.task_id,
        preview=preview,
        explanation=explanation,
    )


# -- confined file access (IL-B drafts, evals D-115) ---------------------------------------------
# Sandboxed test code may plant symlinks in the worktree, or swap a folder for one while a tool
# runs. In a confined session every in-process read, write and delete walks the path one folder
# at a time from the project root with O_NOFOLLOW, so no symlink is ever followed, even one
# swapped in after ``ctx.resolve`` checked the path. Other sessions keep plain file access.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _refuse(path: Path) -> NoReturn:
    raise PathOutsideProjectError(f"path refused (symlink in path): {path}")


def _open_parent(ctx: ToolContext, path: Path, *, create: bool = False) -> tuple[int, str]:
    """A descriptor of ``path``'s folder, opened component by component without following a
    symlink (``create`` makes missing folders), and the final name."""
    root = ctx.project_root.resolve()
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        _refuse(path)
    if not parts or ".." in parts:
        _refuse(path)
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o777, dir_fd=fd)
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    _refuse(path)
                raise
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def _is_link(dfd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def read_bytes(ctx: ToolContext, path: Path) -> bytes:
    """``path``'s bytes; in a confined session never through a symlink, and only from a
    regular file (a planted FIFO cannot block)."""
    if not ctx.confined:
        return Path(path).read_bytes()
    dfd, name = _open_parent(ctx, path)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=dfd)  # fmt: skip
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _refuse(path)
            raise
    finally:
        os.close(dfd)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise IsADirectoryError(f"not a regular file: {path}")
        return fh.read()


def read_text(ctx: ToolContext, path: Path) -> str:
    return read_bytes(ctx, path).decode("utf-8", errors="replace")


def write_text(ctx: ToolContext, path: Path, content: str) -> None:
    """Atomic write; in a confined session the temp file is created and renamed inside a
    folder opened without following symlinks, and a symlink at ``path`` is refused."""
    if not ctx.confined:
        _atomic_write(Path(path), content)
        return
    dfd, name = _open_parent(ctx, path, create=True)
    tmp = f".trendlab-{secrets.token_hex(8)}"
    try:
        if _is_link(dfd, name):
            _refuse(path)
        try:
            mode = os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(tmp, flags, mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dfd)
    finally:
        os.close(dfd)


def unlink(ctx: ToolContext, path: Path) -> None:
    if not ctx.confined:
        Path(path).unlink()
        return
    dfd, name = _open_parent(ctx, path)
    try:
        if _is_link(dfd, name):
            _refuse(path)
        os.unlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)


# -- read-only -------------------------------------------------------------------------------
class ReadFileInput(BaseModel):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file inside the project, optionally a line range (default: up to 400 lines). "
        "Returns numbered lines and the file's sha256 for later guarded writes."
    )
    input_model = ReadFileInput

    def permission(self, args: ReadFileInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.READ_ONLY,
            f"Read {args.path}",
            args.model_dump(),
        )

    async def run(self, args: ReadFileInput, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.is_file():
            return ToolResult(ok=False, output=f"not a file: {args.path}")
        raw = read_bytes(ctx, path)
        if _is_binary(raw):
            return ToolResult(
                ok=False, output=f"{args.path} is binary ({len(raw)} bytes); not readable as text"
            )
        if len(raw) > MAX_FILE_BYTES and args.end_line is None:
            return ToolResult(
                ok=False,
                output=f"{args.path} is {len(raw)} bytes; read it in ranges (start_line/end_line)",
            )
        lines = raw.decode("utf-8", errors="replace").splitlines()
        end = args.end_line or min(len(lines), args.start_line + 399)
        chunk = lines[args.start_line - 1 : end]
        numbered = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start=args.start_line))
        footer = (
            ""
            if end >= len(lines)
            else f"\n... ({len(lines) - end} more lines; total {len(lines)})"
        )
        return ToolResult(
            ok=True,
            output=numbered + footer,
            data={"total_lines": len(lines), "sha256": _sha256(raw)},
        )


class ListDirectoryInput(BaseModel):
    path: str = "."


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List entries of a directory inside the project (ignored paths are hidden)."
    input_model = ListDirectoryInput

    def permission(self, args: ListDirectoryInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.READ_ONLY,
            f"List {args.path}",
            args.model_dump(),
        )

    async def run(self, args: ListDirectoryInput, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.is_dir():
            return ToolResult(ok=False, output=f"not a directory: {args.path}")
        root = ctx.project_root.resolve()
        rules = ctx.ignore_rules or IgnoreRules([])
        entries = []
        for e in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            rel = e.relative_to(root).as_posix()
            if rules.ignored(rel, is_dir=e.is_dir()):
                continue
            entries.append(f"{e.name}/" if e.is_dir() else e.name)
        return ToolResult(ok=True, output="\n".join(entries[:500]) or "(empty)")


class GlobInput(BaseModel):
    pattern: str = Field(description="Glob such as 'src/**/*.py' or '*.toml'")
    max_results: int = Field(default=200, ge=1, le=2000)


class GlobTool(Tool):
    name = "glob"
    description = "Find project files by glob pattern, respecting ignore rules."
    input_model = GlobInput

    def permission(self, args: GlobInput, ctx: ToolContext) -> PermissionRequest:
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.READ_ONLY,
            summary=f"Glob {args.pattern}",
            cwd=str(ctx.project_root),
            args=args.model_dump(),
            task_id=ctx.task_id,
        )

    async def run(self, args: GlobInput, ctx: ToolContext) -> ToolResult:
        root = ctx.project_root.resolve()
        rules = ctx.ignore_rules or IgnoreRules([])
        regex = _glob_regex(args.pattern)
        stop = threading.Event()
        state = {"scanned": 0, "timed_out": False}

        def work() -> list[str]:
            hits: list[str] = []
            deadline = time.monotonic() + SEARCH_DEADLINE_S
            for _path, rel in _iter_files(root, root, rules, stop):
                state["scanned"] += 1
                if regex.match(rel):
                    hits.append(rel)
                    if len(hits) >= args.max_results:
                        break
                if time.monotonic() > deadline:
                    state["timed_out"] = True
                    break
            return sorted(hits)

        hits = await _in_thread(work, stop)
        out = "\n".join(hits) or "no matches"
        if state["timed_out"]:
            out += (
                f"\n... stopped after {SEARCH_DEADLINE_S:.0f}s ({state['scanned']} files looked "
                "at): the project is very large; use a narrower pattern such as 'src/**/*.py'"
            )
        return ToolResult(
            ok=True,
            output=out,
            data={"count": len(hits), "timed_out": state["timed_out"]},
        )


class SearchTextInput(BaseModel):
    pattern: str = Field(description="Regular expression")
    path: str = "."
    max_results: int = Field(default=100, ge=1, le=1000)
    case_sensitive: bool = True


class SearchTextTool(Tool):
    name = "search_text"
    description = (
        "Search project files for a regex (uses ripgrep when available). Returns path:line: text."
    )
    input_model = SearchTextInput

    def permission(self, args: SearchTextInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.READ_ONLY,
            f"Search for /{args.pattern}/",
            args.model_dump(),
            affected=[],
        )

    async def run(self, args: SearchTextInput, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(args.path)
        project = ctx.project_root.resolve()
        try:
            flags = 0 if args.case_sensitive else re.IGNORECASE
            regex = re.compile(args.pattern, flags)
        except re.error as exc:
            return ToolResult(ok=False, output=f"invalid regex: {exc}")
        rg = shutil.which("rg")
        if rg and root.is_dir():
            found = await _rg_search(rg, args, root, project, ctx)
            if found is not None:
                return _timed(_search_result(found[0], args.max_results), found[1], None)
        rules = ctx.ignore_rules or IgnoreRules([])
        stop = threading.Event()
        state = {"scanned": 0, "timed_out": False}

        def files():
            if root.is_file():
                yield root, root.relative_to(project).as_posix()
            else:
                yield from _iter_files(root, project, rules, stop)

        def work() -> list[str]:
            hits: list[str] = []
            deadline = time.monotonic() + SEARCH_DEADLINE_S
            for file, rel in files():
                if time.monotonic() > deadline:
                    state["timed_out"] = True
                    break
                if file.suffix.lower() in _SKIP_EXT:
                    continue
                try:
                    if file.stat().st_size > MAX_SEARCH_BYTES:
                        continue
                    raw = read_bytes(ctx, file)
                except (OSError, PathOutsideProjectError):
                    continue
                state["scanned"] += 1
                if _is_binary(raw):
                    continue
                text = raw.decode("utf-8", errors="replace")
                for n, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                        if len(hits) > args.max_results:
                            return hits
            return hits

        hits = await _in_thread(work, stop)
        return _timed(_search_result(hits, args.max_results), state["timed_out"], state["scanned"])


# ripgrep reads only what it is pointed at: never a config file (RIPGREP_CONFIG_PATH could add
# --follow or --pre) and never through a symlink.
RG_HARDEN = ("--no-config", "--no-follow")


def _rg_in_sandbox(rg: str) -> bool:
    """Is this ripgrep binary visible inside the confined sandbox (a system folder)?"""
    real = os.path.realpath(rg)
    return any(real.startswith(f"{d}/") for d in SYSTEM_RO if d != "/etc")


async def _rg_search(
    rg: str, args: SearchTextInput, root: Path, project: Path, ctx: ToolContext
) -> tuple[list[str], bool] | None:
    cmd = [
        rg,
        *RG_HARDEN,
        "--no-heading",
        "--line-number",
        "--color",
        "never",
        "--max-count",
        "50",
        "--max-filesize",
        "2M",
    ]
    if not args.case_sensitive:
        cmd.append("-i")
    for ex in ("node_modules", ".venv", ".git", "__pycache__", ".trendlab", "dist", "build"):
        cmd += ["--glob", f"!{ex}"]
    cmd += ["-e", args.pattern, str(root)]
    env = {k: v for k, v in os.environ.items() if k != "RIPGREP_CONFIG_PATH"}
    if ctx.confined and _rg_in_sandbox(rg):
        # a draft (evals D-115): ripgrep runs inside the tool sandbox like every other command
        try:
            cmd = ctx.sandbox.wrap(shlex.join(cmd), cwd=project)
        except RuntimeError:
            return None  # the Python search (no symlinks, confined reads) answers instead
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=ctx.confined,
        )
    except OSError:
        return None
    raw: list[str] = []
    deadline = time.monotonic() + SEARCH_DEADLINE_S
    timed_out = False
    try:
        assert proc.stdout is not None
        # read as ripgrep finds matches and stop once there are enough: a huge tree with a
        # common pattern answers in a moment instead of being searched to the end
        while len(raw) <= args.max_results:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=left)
            except TimeoutError:
                timed_out = True
                break
            if not line:
                break
            raw.append(line.decode("utf-8", "replace").rstrip("\n"))
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    if not timed_out and len(raw) <= args.max_results and proc.returncode not in {0, 1, -9}:
        return None
    hits = []
    for line in raw:
        path, _, rest = line.partition(":")
        try:
            rel = Path(path).relative_to(project).as_posix()
        except ValueError:
            rel = path
        hits.append(f"{rel}:{rest[:220]}")
    return hits, timed_out


def _timed(result: ToolResult, timed_out: bool, scanned: int | None) -> ToolResult:
    if timed_out:
        files = f" ({scanned} files searched)" if scanned is not None else ""
        result.output += (
            f"\n... stopped after {SEARCH_DEADLINE_S:.0f}s{files}: the project is very large; "
            "pass path= to search one folder"
        )
        result.data["timed_out"] = True
    return result


def _search_result(hits: list[str], limit: int) -> ToolResult:
    truncated = len(hits) > limit
    shown = hits[:limit]
    out = "\n".join(shown) or "no matches"
    if truncated:
        out += f"\n... more matches truncated (showing {limit}); narrow the pattern or path"
    return ToolResult(ok=True, output=out, data={"count": len(shown), "truncated": truncated})


# -- mutations -------------------------------------------------------------------------------
def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trendlab-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class WriteFileInput(BaseModel):
    path: str
    content: str
    expected_sha256: str | None = Field(
        default=None,
        description="sha256 of the current content (from read_file); refused if it differs.",
    )
    explanation: str = Field(
        default="", description="Why this change is needed (shown to the approver)."
    )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create or overwrite a project file atomically. Prefer patch_file for edits. "
        "Pass expected_sha256 to guard against external edits."
    )
    input_model = WriteFileInput

    def permission(self, args: WriteFileInput, ctx: ToolContext) -> PermissionRequest:
        preview = None
        try:
            target = ctx.resolve(args.path)
            old = read_text(ctx, target) if target.is_file() else ""
            preview = unified_diff(args.path, old, args.content)
            added, removed = diff_stats(preview)
            summary = f"{'Edit' if old else 'Create'} {args.path} (+{added} -{removed})"
        except PathOutsideProjectError:
            summary = f"Write {args.path}"
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.PROJECT_WRITE,
            summary,
            args.model_dump(exclude={"explanation"}),
            preview=preview,
            explanation=args.explanation[:300],
        )

    async def run(self, args: WriteFileInput, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        old = ""
        if path.exists():
            current = read_bytes(ctx, path)
            if args.expected_sha256 and _sha256(current) != args.expected_sha256:
                return ToolResult(
                    ok=False, output="conflict: file changed since it was read; read it again"
                )
            old = current.decode("utf-8", errors="replace")
            if old == args.content:  # idempotent: same content again is a no-op, not a change
                return ToolResult(
                    ok=True,
                    output=f"{args.path} already has exactly this content (no change)",
                    data={"sha256": _sha256(current), "unchanged": True, "path": args.path},
                )
        write_text(ctx, path, args.content)
        diff = unified_diff(args.path, old, args.content)
        added, removed = diff_stats(diff)
        return ToolResult(
            ok=True,
            output=f"wrote {args.path} (+{added} -{removed})",
            data={"sha256": _sha256(args.content.encode()), "diff": diff, "path": args.path},
        )


class PatchFileInput(BaseModel):
    path: str
    old_text: str = Field(
        description="Exact text to replace; must occur exactly once (or set replace_all)."
    )
    new_text: str
    replace_all: bool = False
    expected_sha256: str | None = None
    explanation: str = Field(
        default="", description="Why this change is needed (shown to the approver)."
    )


class PatchFileTool(Tool):
    name = "patch_file"
    description = (
        "Apply a targeted edit: replace old_text with new_text in a project file. old_text must "
        "match "
        "exactly once unless replace_all is true. Returns the resulting diff."
    )
    input_model = PatchFileInput

    def _apply(self, content: str, args: PatchFileInput) -> tuple[str | None, str | None]:
        count = content.count(args.old_text)
        if count == 0:
            return None, "old_text not found in file; read the file and copy the exact text"
        if count > 1 and not args.replace_all:
            return None, f"old_text occurs {count} times; include more context or set replace_all"
        if args.replace_all:
            return content.replace(args.old_text, args.new_text), None
        return content.replace(args.old_text, args.new_text, 1), None

    def permission(self, args: PatchFileInput, ctx: ToolContext) -> PermissionRequest:
        preview = None
        summary = f"Patch {args.path}"
        try:
            target = ctx.resolve(args.path)
            if target.is_file():
                old = read_text(ctx, target)
                new, err = self._apply(old, args)
                if new is not None:
                    preview = unified_diff(args.path, old, new)
                    added, removed = diff_stats(preview)
                    summary = f"Patch {args.path} (+{added} -{removed})"
                else:
                    preview = f"(patch would fail: {err})"
        except PathOutsideProjectError:
            pass
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.PROJECT_WRITE,
            summary,
            args.model_dump(exclude={"explanation"}),
            preview=preview,
            explanation=args.explanation[:300],
        )

    async def run(self, args: PatchFileInput, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.is_file():
            return ToolResult(ok=False, output=f"not a file: {args.path}")
        raw = read_bytes(ctx, path)
        if args.expected_sha256 and _sha256(raw) != args.expected_sha256:
            return ToolResult(
                ok=False, output="conflict: file changed since it was read; read it again"
            )
        old = raw.decode("utf-8", errors="replace")
        if (
            args.old_text not in old
            and args.new_text
            and args.new_text in old
            and args.old_text not in args.new_text
        ):
            # idempotent: this exact patch is already in the file (e.g. a retried call)
            return ToolResult(
                ok=True,
                output=f"{args.path} already contains this change (no change)",
                data={"sha256": _sha256(raw), "unchanged": True, "path": args.path},
            )
        new, err = self._apply(old, args)
        if new is None:
            return ToolResult(ok=False, output=f"patch failed: {err}")
        write_text(ctx, path, new)
        diff = unified_diff(args.path, old, new)
        added, removed = diff_stats(diff)
        return ToolResult(
            ok=True,
            output=f"patched {args.path} (+{added} -{removed})\n{diff}",
            data={"sha256": _sha256(new.encode()), "diff": diff, "path": args.path},
        )


class DeleteFileInput(BaseModel):
    path: str


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a single project file."
    input_model = DeleteFileInput

    def permission(self, args: DeleteFileInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.FILE_DELETE,
            f"Delete {args.path}",
            args.model_dump(),
        )

    async def run(self, args: DeleteFileInput, ctx: ToolContext) -> ToolResult:
        path: Path = ctx.resolve(args.path)
        if not path.is_file():
            return ToolResult(ok=False, output=f"not a file: {args.path}")
        unlink(ctx, path)
        return ToolResult(ok=True, output=f"deleted {args.path}", data={"path": args.path})
