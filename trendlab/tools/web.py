"""Web tools (spec §64): ``web_fetch`` and ``web_search`` as NETWORK-category operations.

Both use httpx, strip HTML to readable text, cap sizes, and run through the permission
engine like every other tool (ask mode prompts, unsafe mode runs). Search uses a configurable
HTML endpoint (DuckDuckGo by default) so no paid API is required.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote_plus, unquote, urlsplit

import httpx
from pydantic import BaseModel, Field

from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.security.redaction import redact_text
from trendlab.tools.base import Tool, ToolContext, ToolResult

DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
_UA = "TrendLab-CLI/0.1 (+https://github.com/antoniowilliams123/trendlab-cli)"
_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "169.254.169.254",
    "metadata.google.internal",
}


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(
        r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article|pre|blockquote)>", "\n", text
    )
    text = re.sub(r"(?i)</(td|th)>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _check_url(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return "only http(s) URLs are allowed"
    host = (parts.hostname or "").lower()
    if (
        not host
        or host in _BLOCKED_HOSTS
        or host.endswith(".local")
        or host.startswith(("10.", "192.168."))
    ):
        return "local and private network addresses are not allowed"
    return None


class WebFetchInput(BaseModel):
    url: str
    max_chars: int = Field(default=12_000, ge=500, le=60_000)


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "Fetch a public web page (docs, an issue, a changelog, an error's source) and return its "
        "text. "
        "Network access: requires approval in ask mode. Private/local addresses are refused."
    )
    input_model = WebFetchInput

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 20.0) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _UA}
        )

    def permission(self, args: WebFetchInput, ctx: ToolContext) -> PermissionRequest:
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.NETWORK,
            summary=f"Fetch {urlsplit(args.url).hostname or args.url}",
            command=f"GET {args.url}"[:200],
            cwd=str(ctx.project_root),
            preview=f"URL that will be requested:\n{args.url}",
            args=args.model_dump(),
            task_id=ctx.task_id,
        )

    async def run(self, args: WebFetchInput, ctx: ToolContext) -> ToolResult:
        problem = _check_url(args.url)
        if problem:
            return ToolResult(ok=False, output=f"refused: {problem}")
        try:
            resp = await self._client.get(args.url)
        except httpx.TimeoutException:
            return ToolResult(ok=False, output=f"timed out fetching {args.url}")
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, output=f"network error: {exc.__class__.__name__}")
        ctype = resp.headers.get("content-type", "")
        body = resp.text
        text = html_to_text(body) if "html" in ctype or body.lstrip().startswith("<") else body
        text = redact_text(text)
        truncated = len(text) > args.max_chars
        text = text[: args.max_chars]
        header = f"{args.url} → HTTP {resp.status_code} ({ctype.split(';')[0] or 'unknown type'})"
        if truncated:
            header += f" — truncated to {args.max_chars} chars"
        return ToolResult(
            ok=resp.status_code < 400,
            output=f"{header}\n\n{text}",
            data={"status": resp.status_code, "truncated": truncated, "url": str(resp.url)},
        )


_RESULT = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)


def parse_duckduckgo(raw: str, limit: int) -> list[dict[str, str]]:
    titles = _RESULT.findall(raw)
    snippets = [html_to_text(s) for s in _SNIPPET.findall(raw)]
    out = []
    for i, (href, title) in enumerate(titles[:limit]):
        url = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            url = unquote(m.group(1))
        out.append(
            {
                "title": html_to_text(title),
                "url": url,
                "snippet": snippets[i] if i < len(snippets) else "",
            }
        )
    return out


class WebSearchInput(BaseModel):
    query: str
    max_results: int = Field(default=6, ge=1, le=15)


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web and return titles, URLs and snippets. Follow up with web_fetch on the best "
        "hit. Network access: requires approval in ask mode."
    )
    input_model = WebSearchInput

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        search_url: str = DEFAULT_SEARCH_URL,
        timeout: float = 20.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _UA}
        )
        self._search_url = search_url

    def permission(self, args: WebSearchInput, ctx: ToolContext) -> PermissionRequest:
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.NETWORK,
            summary=f"Web search: {args.query[:60]}",
            command=f"search {args.query}"[:200],
            cwd=str(ctx.project_root),
            preview=f"Query that will be sent:\n{args.query}",
            args=args.model_dump(),
            task_id=ctx.task_id,
        )

    async def run(self, args: WebSearchInput, ctx: ToolContext) -> ToolResult:
        url = self._search_url.format(query=quote_plus(args.query))
        try:
            resp = await self._client.get(url)
        except httpx.TimeoutException:
            return ToolResult(ok=False, output="search timed out")
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, output=f"network error: {exc.__class__.__name__}")
        if resp.status_code >= 400:
            return ToolResult(ok=False, output=f"search endpoint returned HTTP {resp.status_code}")
        results = parse_duckduckgo(resp.text, args.max_results)
        if not results:
            return ToolResult(ok=True, output="no results", data={"results": []})
        lines = [
            f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet'][:200]}"
            for i, r in enumerate(results)
        ]
        return ToolResult(ok=True, output="\n".join(lines), data={"results": results})
