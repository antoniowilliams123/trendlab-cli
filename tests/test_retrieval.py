"""Lexical retrieval: tokenizer, chunking, BM25 ranking, hints, injection, ranked session search,
recall floor on the suite."""

import tempfile
from pathlib import Path

from trendlab.benchmarks.suite import TASKS, materialize
from trendlab.config.schema import PermissionMode
from trendlab.context.ignore import IgnoreRules
from trendlab.context.retrieval import (
    BM25,
    Chunk,
    chunk_file,
    hints_message,
    index_project,
    search,
    tokens,
)
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent


def test_tokens_split_identifiers():
    assert tokens("busiest_hours loadOrders HTTPServer") == [
        "busiest",
        "hour",
        "load",
        "order",
        "http",
        "server",
    ]
    # light stemming: a requirement's "reserving" meets the code's "reserve"
    assert tokens("reserving reserved reserves reserve") == ["reserv"] * 4
    assert set(tokens("stopped stopping")) == {"stop"}
    assert "fix" in tokens("fix the bug") and "fix" not in tokens("fix the bug", code=True)


def test_chunking_and_ranking():
    src = "import os\n\n\ndef add(a, b):\n    return a + b\n\n\nclass Stock:\n    def low(self):\n        return []\n"
    ch = chunk_file("m.py", src)
    names = sorted((c.start, c.end) for c in ch)
    assert (4, 5) in names and (8, 10) in names and (1, 3) in names
    bm = BM25([["stock", "low"], ["add", "sum"], ["stock", "level"]])
    s = bm.scores(["stock", "low"])
    assert s[0] > s[2] > s[1] == 0.0
    hits = search(ch, "low stock alert is empty")
    assert hits[0][0].start == 8
    msg = hints_message(hits)
    assert msg.startswith("Retrieved code") and "m.py:8-10" in msg
    assert hints_message([]) == "" and search([], "x") == []


def test_recall_floor_on_the_suite():
    n = hits3 = 0
    for t in TASKS:
        if t.lang == "go" or t.answer_keywords:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "r"
            materialize(t, root)
            files = [c.path for c, _ in search(index_project(root, IgnoreRules([])), t.prompt, k=3)]
            n += 1
            hits3 += t.answer_file in files
    assert hits3 / n >= 0.75, hits3 / n


async def test_retrieval_injected_as_labelled_hint(
    project: Path, manager_factory, events, recorder
):
    (project / "src/stock.py").write_text(
        "def low(levels, threshold=5):\n    return [k for k, v in levels.items() if v < threshold]\n"
    )
    provider = ScriptedProvider([ModelResponse(text="low() uses < instead of <=.")])
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    agent.retriever = lambda text: (
        hints_message(h := search(index_project(project, IgnoreRules([])), text)),
        h,
    )
    await agent.run("low stock misses items exactly at the threshold")
    ev = recorder.of_type(EventType.RETRIEVAL_INJECTED)[0].data
    assert ev["hits"][0].startswith("src/stock.py:1-")
    injected = [
        m
        for m in agent.messages
        if m.get("role") == "user" and str(m.get("content")).startswith("Retrieved code")
    ]
    assert len(injected) == 1 and "not certain" in injected[0]["content"]


def test_ranked_session_search(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    a = store.create_session("/p", "m")
    b = store.create_session("/p", "m")
    store.append_message(
        a, {"role": "user", "content": "the telegram bridge stops responding after a while"}
    )
    store.append_message(b, {"role": "user", "content": "add a csv export to the report"})
    top = store.ranked_search("telegram bridge not responding")
    assert top[0]["session_id"] == a and (len(top) == 1 or top[0]["score"] > top[1]["score"])
    assert store.ranked_search("zzzz qqqq") == []
    store.close()
    assert isinstance(Chunk("x", 1, 1, ""), Chunk)


def test_index_cache_reuses_unchanged_files(tmp_path):
    import os

    from trendlab.context.retrieval import index_project

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    first = index_project(tmp_path, IgnoreRules([]))
    assert (tmp_path / ".trendlab/index/chunks.json").is_file()
    (tmp_path / "b.py").write_text("def gamma():\n    return 3\n")
    st = (tmp_path / "b.py").stat()
    os.utime(tmp_path / "b.py", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    second = index_project(tmp_path, IgnoreRules([]))
    texts = {c.text.split("(")[0] for c in second}
    assert texts == {"def alpha", "def gamma"} and len(first) == len(second)
