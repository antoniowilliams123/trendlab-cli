"""Local HTTP stub server (cheap-model spec §8.5).

``trendlab stub --spec openapi.yaml`` answers every path in the spec with its example (or a
minimal value built from the response schema); ``--spec recorded.json`` replays recorded
exchanges; ``--record HOST`` proxies an allow-listed host once and writes the recording. Tests
that call external services point at the stub instead of the real world.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _load_spec(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("YAML specs need the 'pyyaml' package; use JSON instead") from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def example_for(schema: dict[str, Any] | None) -> Any:
    """A minimal value matching a JSON schema (examples win when present)."""
    if not schema:
        return {}
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {k: example_for(v) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return [example_for(schema.get("items"))]
    return {"string": "string", "integer": 0, "number": 0.0, "boolean": False}.get(t, None)


def routes_from_openapi(spec: dict[str, Any]) -> list[tuple[str, re.Pattern[str], int, Any]]:
    """(method, path regex, status, body) for every operation; the first 2xx response is used."""
    routes = []
    for path, ops in (spec.get("paths") or {}).items():
        rx = re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", path) + "/?$")
        for method, op in (ops or {}).items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            responses = op.get("responses") or {}
            status, body = 200, {}
            for code, resp in sorted(responses.items()):
                if str(code).startswith("2"):
                    status = int(code)
                    content = (resp or {}).get("content") or {}
                    media = content.get("application/json") or next(iter(content.values()), {})
                    body = media.get("example") or example_for(media.get("schema"))
                    break
            routes.append((method.upper(), rx, status, body))
    return routes


def routes_from_recording(rec: dict[str, Any]) -> list[tuple[str, re.Pattern[str], int, Any]]:
    routes = []
    for ex in rec.get("exchanges") or []:
        routes.append(
            (
                ex.get("method", "GET").upper(),
                re.compile("^" + re.escape(ex["path"]) + "$"),
                int(ex.get("status", 200)),
                ex.get("body"),
            )
        )
    return routes


class StubServer:
    def __init__(
        self,
        *,
        spec: Path | None = None,
        port: int = 0,
        record_host: str | None = None,
        record_to: Path | None = None,
    ) -> None:
        self.routes: list[tuple[str, re.Pattern[str], int, Any]] = []
        self.exchanges: list[dict[str, Any]] = []
        self.record_host = record_host
        self.record_to = record_to
        if spec is not None:
            data = _load_spec(spec)
            self.routes = (
                routes_from_recording(data) if "exchanges" in data else routes_from_openapi(data)
            )
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:  # quiet
                pass

            def _serve(self) -> None:
                body_in = b""
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body_in = self.rfile.read(length)
                status, body = server.answer(self.command, self.path, body_in)
                payload = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def answer(self, method: str, path: str, body_in: bytes) -> tuple[int, Any]:
        for m, rx, status, body in self.routes:
            if m == method.upper() and rx.match(path.split("?")[0]):
                return status, body
        if self.record_host:
            return self._record(method, path, body_in)
        return 404, {"error": f"no stub for {method} {path}"}

    def _record(self, method: str, path: str, body_in: bytes) -> tuple[int, Any]:
        req = urllib.request.Request(
            f"https://{self.record_host}{path}", data=body_in or None, method=method
        )
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - allow-listed host
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except Exception as exc:  # noqa: BLE001
            return 502, {"error": f"record failed: {exc.__class__.__name__}"}
        try:
            body: Any = json.loads(raw)
        except ValueError:
            body = raw
        ex = {"method": method.upper(), "path": path, "status": status, "body": body}
        self.exchanges.append(ex)
        self.routes.append((method.upper(), re.compile("^" + re.escape(path) + "$"), status, body))
        if self.record_to:
            self.record_to.write_text(json.dumps({"exchanges": self.exchanges}, indent=1))
        return status, body

    def start(self) -> StubServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
