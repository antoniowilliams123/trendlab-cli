import pytest

from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.models import OperationCategory
from trendlab.providers.base import ToolCall
from trendlab.tools.apply_patch import PatchError, apply_hunks, parse_unified_diff
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.ui.diff_view import unified_diff

ORIGINAL = "import os\n\nTIMEOUT = 30\n\ndef run():\n    return TIMEOUT\n"


def test_parse_and_apply_single_hunk():
    new = ORIGINAL.replace("TIMEOUT = 30", "TIMEOUT = 60")
    diff = unified_diff("src/app.py", ORIGINAL, new)
    [fp] = parse_unified_diff(diff)
    assert fp.old_path == "src/app.py" and fp.new_path == "src/app.py" and len(fp.hunks) == 1
    assert apply_hunks(ORIGINAL, fp.hunks) == new


def test_apply_with_offset_and_whitespace_fuzz():
    new = ORIGINAL.replace("return TIMEOUT", "return TIMEOUT * 2")
    diff = unified_diff("a.py", ORIGINAL, new)
    [fp] = parse_unified_diff(diff)
    shifted = "# header\n# header 2\n" + ORIGINAL  # hunk line numbers are now off by two
    assert apply_hunks(shifted, fp.hunks) == "# header\n# header 2\n" + new
    reindented = ORIGINAL.replace("    return TIMEOUT", "\treturn TIMEOUT")
    out = apply_hunks(reindented, fp.hunks)
    assert "return TIMEOUT * 2" in out


def test_mismatch_is_rejected():
    diff = unified_diff("a.py", ORIGINAL, ORIGINAL.replace("30", "60"))
    [fp] = parse_unified_diff(diff)
    with pytest.raises(PatchError, match="does not match"):
        apply_hunks("completely different\ncontent\n", fp.hunks)
    with pytest.raises(PatchError, match="no file headers"):
        parse_unified_diff("@@ -1 +1 @@\n-a\n+b\n")


async def test_tool_multi_file_create_and_delete(project, manager_factory, events, recorder):
    mgr = manager_factory()
    (project / "src" / "old.py").write_text("gone\n")
    diff = (
        unified_diff("src/app.py", "TIMEOUT = 30\n", "TIMEOUT = 45\n")
        + unified_diff("src/new_mod.py", "", "def new():\n    return 1\n").replace(
            "--- a/src/new_mod.py", "--- /dev/null"
        )
        + "--- a/src/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    )
    engine = PermissionEngine(PermissionMode.ASK)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    rt = ToolRuntime(default_registry(), engine, mgr, events, ctx)
    tool = rt.registry.get("apply_patch")
    perm = tool.permission(tool.parse({"diff": diff, "explanation": "bump + add + remove"}), ctx)
    assert perm.category == OperationCategory.FILE_DELETE  # a deletion is in the patch → high risk
    assert set(perm.affected_files) == {"src/app.py", "src/new_mod.py", "src/old.py"}
    assert (
        "+TIMEOUT = 45" in perm.preview
        and "-gone" in perm.preview
        and perm.summary.startswith("Apply patch to 3 file(s)")
    )
    engine.mode = PermissionMode.UNSAFE
    engine.allow_destructive = True
    res = await rt.execute(ToolCall(id="1", name="apply_patch", arguments={"diff": diff}))
    assert res.ok, res.output
    assert (project / "src" / "app.py").read_text() == "TIMEOUT = 45\n"
    assert (project / "src" / "new_mod.py").read_text() == "def new():\n    return 1\n"
    assert not (project / "src" / "old.py").exists()
    assert set(rt.changed_files) == {"src/app.py", "src/new_mod.py", "src/old.py"}


async def test_tool_is_all_or_nothing(project, manager_factory, events):
    mgr = manager_factory()
    good = unified_diff("src/app.py", "TIMEOUT = 30\n", "TIMEOUT = 31\n")
    bad = unified_diff("README.md", "not the real content\n", "changed\n")
    engine = PermissionEngine(PermissionMode.UNSAFE)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    rt = ToolRuntime(default_registry(), engine, mgr, events, ctx)
    res = await rt.execute(ToolCall(id="1", name="apply_patch", arguments={"diff": good + bad}))
    assert not res.ok and "nothing written" in res.output
    assert (project / "src" / "app.py").read_text() == "TIMEOUT = 30\n"
    res = await rt.execute(
        ToolCall(id="2", name="apply_patch", arguments={"diff": unified_diff("../x.py", "", "y\n")})
    )
    assert res.output.startswith("DENIED")
