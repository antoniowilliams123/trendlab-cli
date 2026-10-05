import asyncio

import httpx

from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.models import OperationCategory
from trendlab.providers.base import ToolCall
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.tools.web import WebFetchTool, WebSearchTool, html_to_text, parse_duckduckgo

PAGE = """<html><head><title>Docs</title><style>x{}</style><script>bad()</script></head>
<body><h1>Install</h1><p>Run <code>pip install thing</code>.</p><p>Token: sk-abcdefghijklmnopqrstuvwxyz12345</p>
<ul><li>one</li><li>two</li></ul></body></html>"""
DDG = """<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&amp;rut=1">Example <b>Docs</b></a>
<a class="result__snippet" href="#">The official docs for <b>example</b>.</a></div>
<div class="result"><a rel="nofollow" class="result__a" href="https://other.dev/page">Other page</a>
<a class="result__snippet" href="#">Another hit.</a></div>"""


def test_html_to_text_and_ddg_parser():
    text = html_to_text(PAGE)
    assert (
        "Install" in text
        and "pip install thing" in text
        and "bad()" not in text
        and "x{}" not in text
    )
    assert "one\n" in text and "two" in text
    results = parse_duckduckgo(DDG, 5)
    assert results[0] == {
        "title": "Example Docs",
        "url": "https://example.com/docs",
        "snippet": "The official docs for example.",
    }
    assert results[1]["url"] == "https://other.dev/page" and len(results) == 2


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def test_web_fetch_strips_html_redacts_and_refuses_private(project):
    def handler(req):
        if req.url.host == "docs.example" and "missing" not in req.url.path:
            body = PAGE + ("<p>" + "word " * 800 + "</p>" if "long" in req.url.path else "")

            return httpx.Response(
                200, text=body, headers={"content-type": "text/html; charset=utf-8"}
            )
        return httpx.Response(404, text="nope")

    tool = WebFetchTool(client=_client(handler))
    ctx = ToolContext(project_root=project, session_id="s")
    perm = tool.permission(tool.parse({"url": "https://docs.example/install"}), ctx)
    assert perm.category == OperationCategory.NETWORK and perm.summary == "Fetch docs.example"
    assert "https://docs.example/install" in perm.preview
    res = await tool.run(tool.parse({"url": "https://docs.example/install"}), ctx)
    assert res.ok and "HTTP 200" in res.output and "pip install thing" in res.output
    assert "sk-abcdef" not in res.output and "[REDACTED]" in res.output and "<p>" not in res.output
    res = await tool.run(tool.parse({"url": "https://docs.example/missing"}), ctx)
    assert not res.ok and "HTTP 404" in res.output
    for bad in (
        "http://localhost:8787/api",
        "http://169.254.169.254/latest",
        "ftp://x/y",
        "http://192.168.1.5/",
    ):
        res = await tool.run(tool.parse({"url": bad}), ctx)
        assert not res.ok and res.output.startswith("refused"), bad
    res = await tool.run(tool.parse({"url": "https://docs.example/long", "max_chars": 500}), ctx)
    assert "truncated to 500" in res.output


async def test_web_search_parses_results_and_handles_errors(project):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        if "fail" in str(req.url):
            return httpx.Response(503)
        return httpx.Response(200, text=DDG)

    tool = WebSearchTool(client=_client(handler))
    ctx = ToolContext(project_root=project, session_id="s")
    res = await tool.run(tool.parse({"query": "example docs install", "max_results": 1}), ctx)
    assert res.ok and "1. Example Docs" in res.output and "https://example.com/docs" in res.output
    assert "q=example+docs+install" in seen["url"] and len(res.data["results"]) == 1
    res = await tool.run(tool.parse({"query": "fail"}), ctx)
    assert not res.ok and "503" in res.output


async def test_web_tools_require_approval_in_ask_mode(project, manager_factory, events):
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.ASK),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    assert rt.registry.get("web_fetch") and rt.registry.get("web_search")
    task = asyncio.create_task(
        rt.execute(ToolCall(id="1", name="web_search", arguments={"query": "x"}))
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if mgr.pending():
            break
    [req] = mgr.pending()
    assert (
        req.summary.startswith("Web search") and req.remote_allowed and req.risk.value == "medium"
    )
    mgr.decide(req.approval_id, "deny", via="local", trusted=True)
    assert "NOT EXECUTED" in (await task).output
