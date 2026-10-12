"""IL-B drafting policy, worker side (evals spec §30.4, §30.5, D-115).

Decides whether a candidate may be drafted now and for which item, under the referee's pause
switch, the IL-0 guard, a verified sandbox, priced models only, the 01:00-05:00 ET blackout and
the per-finding, daily and monthly dollar caps. Everything fails closed. Nothing here edits code,
spends money or runs a model.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trendlab.config.schema import AppConfig, PermissionMode, SandboxConfig
from trendlab.engine.inbox import Inbox, split_signature
from trendlab.engine.meta import HARNESS_PROJECT
from trendlab.engine.observe import CASE_ID_RE, RUN_ID_RE
from trendlab.security.sandbox import GIT_HARDEN_ARGS, GIT_HARDEN_ENV, Sandbox, bwrap_position

NY = ZoneInfo("America/New_York")
SWITCH = "improve.json"
CONSUMED = "candidates-consumed.json"
MAX_CONTROL_BYTES = 64 * 1024
MIN_BUDGET_USD = 0.05
BLACKOUT = (1, 5)  # 01:00-05:00 America/New_York: no loop activity
PROBE_TIMEOUT_S = 20
PROBE_FILE = ".ilb-sandbox-probe"
SCRATCH_HOME = "/tmp/home"  # on the sandbox's private /tmp
DRAFT_MAX_OUTPUT_TOKENS = 8192  # every draft model call's hard output bound
DENIED_TOOLS = ("delegate", "web_fetch", "web_search")
SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2}
_REF = re.compile(r"^evals:([^:]+):([^:]+)$")
_CID = re.compile(r"^cand-[A-Za-z0-9]+-\d+$")


def ny_now() -> datetime:
    return datetime.now(NY)


def ny_day(now: datetime) -> str:
    return now.astimezone(NY).strftime("%Y-%m-%d")


# -- referee-owned control files -----------------------------------------------------------------
@dataclass(frozen=True)
class Switch:
    paused: bool
    no_autofix: tuple[str, ...] = ()
    reason: str = ""


def _read_control(path: Path) -> Any:
    """JSON from a referee control file: no symlinks, a regular file, at most 64 KiB."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        data = os.read(fd, MAX_CONTROL_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_CONTROL_BYTES:
        raise ValueError("larger than 64 KiB")
    return json.loads(data)


def read_switch(control_dir: str | Path) -> Switch:
    """The referee's pause switch. Missing, unreadable or malformed means paused."""
    try:
        data = _read_control(Path(control_dir) / SWITCH)
    except FileNotFoundError:
        return Switch(True, (), "no switch file (paused until the referee resumes)")
    except (OSError, ValueError) as exc:
        return Switch(True, (), f"switch unreadable: {exc.__class__.__name__}")
    if not isinstance(data, dict):
        return Switch(True, (), "switch is not an object")
    extra = set(data) - {"schema", "paused", "no_autofix", "updated_at", "why"}
    if extra or data.get("schema") != 1 or type(data.get("paused")) is not bool:
        return Switch(True, (), "switch has the wrong schema")
    for key in ("updated_at", "why"):
        if key in data and not (isinstance(data[key], str) and len(data[key]) <= 200):
            return Switch(True, (), "switch has the wrong schema")
    prefixes = data.get("no_autofix")
    if not isinstance(prefixes, list) or not all(
        isinstance(p, str) and len(p) <= 200 and p.startswith("evals|") for p in prefixes
    ):
        return Switch(True, (), "switch has a malformed no_autofix list")
    if data["paused"]:
        return Switch(True, tuple(prefixes), "paused by the referee")
    return Switch(False, tuple(prefixes), "")


def read_consumed(control_dir: str | Path) -> frozenset[str]:
    """Candidate ids the referee has consumed (IL-C writes ``candidates-consumed.json``).
    Missing means none; malformed also means none, so a bad file can only make the worker
    more conservative (more pending hand-offs, never fewer)."""
    try:
        data = _read_control(Path(control_dir) / CONSUMED)
    except (OSError, ValueError):
        return frozenset()
    ids = data.get("consumed") if isinstance(data, dict) and data.get("schema") == 1 else None
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(i for i in ids if isinstance(i, str) and _CID.match(i))


# -- the stripped draft config --------------------------------------------------------------------
def draft_config(config: AppConfig, budget_usd: float, minutes: int) -> AppConfig:
    """A deep copy with every path that could spend past the cap or reach outside turned off.
    The daemon's own config is never touched."""
    cfg = config.model_copy(deep=True)
    cfg.verification.workspace = "inplace"
    cfg.verification.second_opinion = None
    cfg.attempts.best_of = 1
    # no sub-agents; no tool that reaches the network from the harness process (D-115)
    for name in DENIED_TOOLS:
        if name not in cfg.tools.deny:
            cfg.tools.deny.append(name)
    cfg.planner.max_calls = 1
    cfg.routing.pop("router", None)
    cfg.limits.max_cost_usd = budget_usd
    cfg.limits.max_wall_clock_minutes = max(1, minutes - 15)
    cfg.limits.max_tokens_per_run = 2_000_000
    cfg.engine.file_failed_runs = False
    cfg.remote_approval.enabled = False
    cfg.remote_approval.telegram = False
    cfg.notifications.enabled = False
    cfg.notifications.provider = "none"
    cfg.telegram_bridge.enabled = False
    cfg.governance.commit_per_turn = False
    cfg.git.auto_commit = False
    cfg.defaults.permission_mode = PermissionMode.UNSAFE
    cfg.hooks = []
    cfg.mcp = {}
    cfg.sandbox = SandboxConfig(mode="on", writable_paths=[], allow_network=False)
    # a hard output bound per call, so each call's worst case is known before it is made
    for pcfg in cfg.providers.values():
        pcfg.max_tokens = min(pcfg.max_tokens, DRAFT_MAX_OUTPUT_TOKENS)
    return cfg


def price_check(config: AppConfig) -> list[str]:
    """Model refs the draft could reach that have no price (an unpriced call costs $0 to the
    tracker, so it would slip past the cap). Empty means every model is priced."""
    refs = [config.defaults.model, *config.routing.values()]
    if config.verification.second_opinion:
        refs.append(config.verification.second_opinion)
    for ref in list(refs):
        refs.extend(config.fallback.get(ref, []))
    for chain in config.fallback.values():
        refs.extend(chain)
    seen: list[str] = []
    for ref in refs:
        if ref and ref not in config.pricing and ref not in seen:
            seen.append(ref)
    return seen


# -- the sandbox gate -----------------------------------------------------------------------------
def secret_masks() -> list[Path]:
    """What no IL-B sandbox may see: the worker's TrendLab home (secrets, sessions, the engine
    runtime folder and its ``engine.sock``), the locked runtime config, ``~/.ssh`` and every
    user's runtime folder (``/run/user``: other sockets). Existing paths only."""
    from trendlab.config.loader import trendlab_home
    from trendlab.config.runtime import runtime_dir

    home = Path.home()
    paths = [trendlab_home(), home / ".trendlab", home / ".ssh", Path("/run/user")]
    rt = runtime_dir()
    if rt is not None:
        paths.append(rt)
    out: list[Path] = []
    for p in paths:
        with contextlib.suppress(OSError):
            if p.exists() and p.resolve() not in out:
                out.append(p.resolve())
    return out


def confined_env(prod_dir: Path | None = None) -> dict[str, str]:
    """The whole environment of a confined sandbox: PATH (the production venv first), a scratch
    HOME on the private /tmp, LANG and git hardening. No API key, no TRENDLAB_* variable."""
    path = "/usr/local/bin:/usr/bin:/bin"
    if prod_dir is not None:
        path = f"{Path(prod_dir) / '.venv' / 'bin'}:{path}"
    env = {"PATH": path, "HOME": SCRATCH_HOME, "LANG": "C.UTF-8", **GIT_HARDEN_ENV}
    pairs = [(GIT_HARDEN_ARGS[i + 1].split("=", 1)) for i in range(0, len(GIT_HARDEN_ARGS), 2)]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return env


def forced_sandbox(
    project_root: Path, ro_paths: list[Path] | None = None, prod_dir: Path | None = None
) -> Sandbox:
    """The only sandbox profile IL-B runs anything in (candidate tests and every drafting-agent
    tool): bubblewrap, no network, no persistent writable path beyond ``project_root``, a
    scrubbed environment, the secrets and sockets masked, and immune to TRENDLAB_SANDBOX in the
    engine's environment. ``ro_paths`` (the production checkout) stay readable even under the
    private /tmp."""
    return Sandbox(
        SandboxConfig(mode="on", writable_paths=[], allow_network=False),
        project_root,
        force_mode="on",
        ro_paths=list(ro_paths or []),
        confined=True,
        env=confined_env(prod_dir),
        masks=secret_masks(),
    )


def kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, wait 5 s, then SIGKILL."""
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def sandbox_probe(home: Path, *, sandbox: Sandbox | None = None) -> tuple[bool, str]:
    """Run a write probe inside the forced sandbox: a write to ``home`` (writable to the worker
    outside the sandbox) must fail, and the probe must exit 0 within 20 s."""
    home = Path(home)
    tmp = Path(tempfile.mkdtemp(prefix="ilb-probe-"))
    marker = home / PROBE_FILE
    try:
        sb = sandbox or forced_sandbox(tmp)
        if sb.mode != "on":
            return False, f"sandbox mode is {sb.mode!r}, not bubblewrap"
        script = f'touch "$1"/{PROBE_FILE} 2>/dev/null && exit 9; exit 0'
        try:
            argv = sb.wrap(shlex.join(["sh", "-c", script, "sh", str(home)]), cwd=tmp)
        except RuntimeError as exc:
            return False, str(exc)[:200]
        bwrap = sb.bwrap and shutil.which(sb.bwrap)
        at = bwrap_position(argv, sb.bwrap)
        if not bwrap or at is None or shutil.which(argv[at]) != bwrap:
            return False, "the probe would not run under bubblewrap"
        if "--unshare-net" not in argv:
            return False, "the probe sandbox keeps the network"
        try:
            proc = subprocess.Popen(argv, cwd=tmp, start_new_session=True)
        except OSError as exc:
            return False, f"probe did not start: {exc.__class__.__name__}"
        try:
            code = proc.wait(timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            return False, "probe timed out"
        if marker.exists():
            with contextlib.suppress(OSError):
                marker.unlink()  # our own marker file only
            return False, "the sandbox let the probe write outside it"
        if code != 0:
            return False, f"probe exited {code}"
        return True, "ok"
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(tmp)  # empty unless the probe wrote into its own project dir


def agent_sandbox_ok() -> tuple[bool, str]:
    """The drafting agent's shell sandbox reads TRENDLAB_SANDBOX; any value but "on" (or
    unset) would loosen it, so drafting is refused while one is set."""
    env = os.environ.get("TRENDLAB_SANDBOX", "").strip().lower()
    if env in ("", "on"):
        return True, "ok"
    return False, f"TRENDLAB_SANDBOX={env} would loosen the agent's sandbox"


# -- eligibility ----------------------------------------------------------------------------------
def _parse_time(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo else None


def newest_case_ref(item: dict[str, Any]) -> tuple[str, str] | None:
    """``(run_id, case_id)`` from the item's newest ``evals:<run>:<case_id>`` ref."""
    for ref in reversed(item.get("evidence_refs") or []):
        m = _REF.match(str(ref))
        if m and RUN_ID_RE.match(m.group(1)) and CASE_ID_RE.match(m.group(2)):
            return m.group(1), m.group(2)
    return None


def case_prefix_of(item: dict[str, Any]) -> str | None:
    """``evals|<split>|<capability>|<case_id>`` for an item with a stored 5-part case
    signature and a matching evidence ref; None otherwise (ineligible)."""
    ref = newest_case_ref(item)
    sig = item.get("signature")
    if ref is None or not sig:
        return None
    parts = split_signature(sig, case_id=ref[1])
    if parts is None:
        return None
    left, case_id, _codes = parts
    return f"{left}|{case_id}"


def cooldown_state(inbox: Inbox, config: AppConfig, prefix: str, now: datetime) -> str:
    """``ok``, ``cooldown`` or ``needs_tony`` for a case prefix with no hand-off."""
    imp = config.improvement
    attempts = inbox.prefix_attempts(prefix)
    if attempts < imp.max_attempts_per_case:
        return "ok"
    if attempts > imp.max_attempts_per_case:
        return "needs_tony"  # the one post-cooldown attempt also failed
    last = _parse_time(inbox.last_attempt_at(prefix))
    if last is None or now < last + timedelta(days=imp.cooldown_days):
        return "cooldown"
    return "ok"


def eligible_items(
    inbox: Inbox, config: AppConfig, *, now: datetime, switch: Switch
) -> list[tuple[dict[str, Any], str]]:
    """Open evals items that may be drafted, with their case prefix, in priority order:
    severity high, med, low, then the oldest ``first_seen``."""
    out: list[tuple[dict[str, Any], str]] = []
    horizon = now - timedelta(days=config.improvement.recent_days)
    for item in inbox.list(HARNESS_PROJECT, status="open", limit=10_000):
        if item.get("source") != "evals":
            continue
        prefix = case_prefix_of(item)
        if prefix is None:
            continue
        seen = _parse_time(item.get("last_seen"))
        if seen is None or seen < horizon:
            continue
        if any(prefix.startswith(p) for p in switch.no_autofix):
            continue
        if inbox.prefix_has_handoff(prefix):
            continue
        if cooldown_state(inbox, config, prefix, now) != "ok":
            continue
        out.append((item, prefix))
    out.sort(key=lambda t: (SEVERITY_ORDER.get(t[0]["severity"], 3), t[0]["first_seen"]))
    return out


def mark_needs_tony(inbox: Inbox, config: AppConfig, *, now: datetime) -> list[str]:
    """Items whose case used every attempt plus the post-cooldown one without a hand-off: one
    observe-source escalation item each (so the digest announces it), then the item itself is
    silenced with feedback ``needs_tony`` and never drafted again. Returns the item ids."""
    marked = []
    for item in inbox.list(HARNESS_PROJECT, status="open", limit=10_000):
        if item.get("source") != "evals":
            continue
        prefix = case_prefix_of(item)
        if prefix is None or inbox.prefix_has_handoff(prefix):
            continue
        if cooldown_state(inbox, config, prefix, now) != "needs_tony":
            continue
        tag = hashlib.sha1(prefix.encode()).hexdigest()[:12]
        cluster = f"ilb-needs-tony-{tag}"
        inbox.record(
            project=HARNESS_PROJECT,
            title="IL-B: case needs Tony after repeated failed candidates",
            signature=cluster,
            source="observe",
            evidence=[f"item:{item['id']}"],
            severity="med",
            cluster=cluster,
        )
        inbox.set_status(item["id"], "silenced", feedback="needs_tony")
        marked.append(item["id"])
    return marked


# -- the decision ---------------------------------------------------------------------------------
@dataclass
class Decision:
    allowed: bool
    reason: str
    item: dict[str, Any] | None = None
    case_prefix: str | None = None
    budget_usd: float = 0.0
    deadline_minutes: int = 0
    gates: dict[str, Any] = field(default_factory=dict)


def blackout_reason(now: datetime, minutes: int) -> str | None:
    local = now.astimezone(NY)
    start, end = BLACKOUT
    if start <= local.hour < end:
        return "inside the 01:00-05:00 ET blackout"
    next_start = local.replace(hour=start, minute=0, second=0, microsecond=0)
    if local.hour >= start:
        next_start += timedelta(days=1)
    if local + timedelta(minutes=minutes) >= next_start:
        return "would run into the 01:00-05:00 ET blackout"
    return None


def decide(
    inbox: Inbox,
    config: AppConfig,
    *,
    now: datetime,
    isolation_ok: bool,
    sandbox_active: bool,
    switch: Switch,
    consumed: frozenset[str] = frozenset(),
    item_id: str | None = None,
) -> Decision:
    """Pure: may a candidate be drafted now, for which item and with what budget."""
    imp = config.improvement
    minutes = imp.max_draft_minutes

    def no(reason: str) -> Decision:
        return Decision(False, reason, deadline_minutes=minutes)

    if not imp.propose:
        return no("propose is off")
    if not isolation_ok:
        return no("isolation not verified (IL-0)")
    if switch.paused:
        return no(f"paused by the referee: {switch.reason}")
    if not sandbox_active:
        return no("sandbox inactive")
    unpriced = price_check(draft_config(config, imp.max_case_usd, minutes))
    if unpriced:
        return no("unpriced model: " + ", ".join(unpriced[:5]))
    blackout = blackout_reason(now, minutes)
    if blackout:
        return no(blackout)
    if inbox.pending_handoffs(consumed) >= imp.max_pending_handoffs:
        return no("too many un-consumed candidates")
    day_left = imp.max_day_usd - inbox.spent_today(ny_day(now))
    if day_left <= 0:
        return no("daily budget spent")
    month_left = imp.max_month_usd - inbox.spent_month(ny_day(now)[:7])
    if month_left <= 0:
        return no("monthly budget spent")
    items = eligible_items(inbox, config, now=now, switch=switch)
    if item_id is not None:
        items = [t for t in items if t[0]["id"] == item_id]
    if not items:
        return no("nothing eligible" + (f": item {item_id} is not eligible" if item_id else ""))
    item, prefix = items[0]
    budget = min(imp.max_case_usd - inbox.prefix_spend(prefix), day_left, month_left)
    if budget < MIN_BUDGET_USD:
        return no("budget too small")
    return Decision(True, "ok", item, prefix, round(budget, 4), minutes)
