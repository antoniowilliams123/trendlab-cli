"""Uplift U17: hallucinated imports and project APIs are caught on the lines an edit adds."""

from pathlib import Path

from trendlab.agent.symbols import added_lines, unresolved
from trendlab.benchmarks import suite as suite_mod
from trendlab.providers.base import ToolCall

HALLUCINATIONS = {
    "from shop.util import chunk_list": "shop.util has no name 'chunk_list'",
    "import shop.helpers": "module shop.helpers does not exist",
    "from shop import util\nutil.split_into_chunks([1], 2)": "has no attribute 'split_into_chunks'",
    "from .util import clamp_value": "shop.util has no name 'clamp_value'",
    "import requests_cache": "module requests_cache is not installed or declared",
}
REAL = [
    "from shop.util import chunks, clamp",
    "import shop.util as u\nu.chunks([1, 2], 1)",
    "from . import util",
    "import json, os.path",
    "from shop.pricing import TAX_RATE",
]


def _repo(tmp: Path) -> Path:
    root = tmp / "repo"
    suite_mod.materialize(suite_mod.get_task("py01-off_by_one"), root)
    sp = root / ".venv/lib/python3.12/site-packages"
    sp.mkdir(parents=True)
    (sp / "pytest").mkdir()  # the project's own environment
    return root


def test_seeded_hallucinations_are_caught_and_real_names_are_not(tmp_path: Path):
    root = _repo(tmp_path)
    base = (root / "shop/report.py").read_text()
    caught = 0
    for snippet, expected in HALLUCINATIONS.items():
        (root / "shop/report.py").write_text(base + "\n" + snippet + "\n")
        probs = unresolved(root, "shop/report.py")
        caught += any(expected in p for p in probs)
    assert caught == len(HALLUCINATIONS)
    for snippet in REAL:
        (root / "shop/report.py").write_text(base + "\n" + snippet + "\n")
        assert unresolved(root, "shop/report.py") == [], snippet


def test_only_added_lines_are_reported(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "shop/report.py").write_text("import shop.ghost\n\nx = 1\nimport shop.phantom\n")
    assert len(unresolved(root, "shop/report.py")) == 2
    diff = "@@ -1,3 +1,4 @@\n import shop.ghost\n \n x = 1\n+import shop.phantom\n"
    assert added_lines(diff) == {4}
    only = unresolved(root, "shop/report.py", only_lines=added_lines(diff))
    assert len(only) == 1 and "shop.phantom" in only[0]


def test_guarded_optional_imports_are_fine(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "shop/report.py").write_text(
        "try:\n    import yaml\nexcept ImportError:\n    yaml = None\n"
    )
    assert unresolved(root, "shop/report.py") == []


async def test_edit_result_tells_the_model(tmp_path: Path, manager_factory, events, recorder):
    from trendlab.config.schema import PermissionMode
    from trendlab.permissions.engine import PermissionEngine
    from trendlab.telemetry.events import EventType
    from trendlab.tools.base import ToolContext
    from trendlab.tools.registry import default_registry
    from trendlab.tools.runtime import ToolRuntime

    root = _repo(tmp_path)
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=root, session_id=mgr.session_id),
    )
    r = await rt.execute(
        ToolCall(
            id="1",
            name="patch_file",
            arguments={
                "path": "shop/report.py",
                "old_text": "def revenue(",
                "new_text": "from shop.util import chunk_list\n\n\ndef revenue(",
            },
        )
    )
    assert r.ok and "UNRESOLVED REFERENCE" in r.output and "chunk_list" in r.output
    ev = [e.data for e in recorder.of_type(EventType.TOOL_POSTCONDITION_FAILED)]
    assert ev and ev[-1]["kind"] == "unresolved_reference"
    clean = await rt.execute(
        ToolCall(
            id="2",
            name="patch_file",
            arguments={"path": "shop/report.py", "old_text": "chunk_list", "new_text": "chunks"},
        )
    )
    assert clean.ok and "UNRESOLVED" not in clean.output
