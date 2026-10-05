"""Web approval channel — a small authenticated HTTP(S) server for the phone.

Threat model: the server is reachable from the LAN/overlay network. Every API
call needs the install's bearer token; a decision additionally needs the
per-request decision token and the operation fingerprint, and is single-use.
The server never executes anything and never exposes anything beyond the
pending approvals' redacted public view.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import socket
import ssl
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from trendlab.approvals.channels.base import ApprovalChannel, ChannelError
from trendlab.approvals.models import ApprovalError, ApprovalRequest, DecisionResult
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.telemetry.events import EventBus, EventType

if TYPE_CHECKING:
    from trendlab.approvals.manager import ApprovalManager

MAX_BODY_BYTES = 16 * 1024


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class WebApprovalChannel(ApprovalChannel):
    name = "web"
    remote = True

    def __init__(self, config: RemoteApprovalConfig, token: str, events: EventBus) -> None:
        self.config = config
        self._token = token
        self.events = events
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._manager: ApprovalManager | None = None
        self._auth_failures: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def tls(self) -> bool:
        return bool(self.config.tls_cert and self.config.tls_key)

    @property
    def bound_port(self) -> int | None:
        return self._server.server_address[1] if self._server else None

    @property
    def url(self) -> str:
        if self.config.public_url:
            return self.config.public_url.rstrip("/")
        scheme = "https" if self.tls else "http"
        host = (
            self.config.host if self.config.host not in {"0.0.0.0", "::"} else socket.gethostname()
        )
        return f"{scheme}://{host}:{self.bound_port or self.config.port}"

    async def start(self, manager: ApprovalManager) -> None:
        if self._server is not None:
            return
        if (
            not _is_loopback(self.config.host)
            and not self.tls
            and not self.config.allow_insecure_http
        ):
            raise ChannelError(
                "refusing to serve approvals over plain HTTP on a non-loopback address; "
                "configure tls_cert/tls_key, or set allow_insecure_http = true if the network "
                "is already encrypted (e.g. Tailscale)"
            )
        self._manager = manager
        channel = self

        class Handler(_ApprovalHandler):
            pass

        Handler.channel = channel  # type: ignore[attr-defined]
        try:
            server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        except OSError as exc:
            raise ChannelError(f"cannot bind {self.config.host}:{self.config.port}: {exc}") from exc
        server.daemon_threads = True
        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                ctx.load_cert_chain(self.config.tls_cert, self.config.tls_key)  # type: ignore[arg-type]
            except (OSError, ssl.SSLError) as exc:
                server.server_close()
                raise ChannelError(f"cannot load TLS certificate: {exc}") from exc
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="trendlab-remote",
            daemon=True,
        )
        self._thread.start()
        self.events.emit(
            EventType.REMOTE_CHANNEL_STARTED,
            session_id=manager.session_id,
            channel=self.name,
            url=self.url,
            tls=self.tls,
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self.events.emit(
            EventType.REMOTE_CHANNEL_STOPPED,
            session_id=self._manager.session_id if self._manager else None,
            channel=self.name,
        )

    async def dispatch(self, request: ApprovalRequest) -> None:
        # Pull model: the phone polls /api/approvals. Dispatch only confirms availability.
        if self._server is None:
            raise ChannelError("remote approval server is not running")

    async def withdraw(self, request: ApprovalRequest, result: DecisionResult) -> None:
        return None

    # -- auth helpers (called from handler threads) ----------------------------------------
    def check_auth(self, header: str | None, client: str) -> bool:
        with self._lock:
            if self._auth_failures.get(client, 0) >= self.config.max_auth_failures:
                return False
        ok = False
        if header and header.startswith("Bearer "):
            ok = hmac.compare_digest(header[7:].strip(), self._token)
        if not ok:
            with self._lock:
                self._auth_failures[client] = self._auth_failures.get(client, 0) + 1
            self.events.emit(
                EventType.REMOTE_AUTH_FAILED,
                session_id=self._manager.session_id if self._manager else None,
                channel=self.name,
                client=client,
            )
        return ok

    def locked_out(self, client: str) -> bool:
        with self._lock:
            return self._auth_failures.get(client, 0) >= self.config.max_auth_failures

    @property
    def manager(self) -> ApprovalManager:
        assert self._manager is not None
        return self._manager


def _load_page() -> bytes:
    return resources.files("trendlab.approvals.channels").joinpath("web_page.html").read_bytes()


class _ApprovalHandler(BaseHTTPRequestHandler):
    channel: WebApprovalChannel
    server_version = "TrendLab"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — silence stdlib logging
        return None

    # -- helpers ---------------------------------------------------------------------------
    def _client(self) -> str:
        return self.client_address[0]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        client = self._client()
        if self.channel.locked_out(client):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "locked_out"})
            return False
        if not self.channel.check_auth(self.headers.get("Authorization"), client):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # -- routes -----------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            body = _load_page()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            if not self._authorized():
                return
            mgr = self.channel.manager
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "machine": mgr.machine,
                    "project": mgr.project_name,
                    "pending": len(mgr.pending(remote_only=True)),
                },
            )
            return
        if path == "/api/approvals":
            if not self._authorized():
                return
            mgr = self.channel.manager
            items = [
                r.public_view(include_decision_token=True) for r in mgr.pending(remote_only=True)
            ]
            local_only = len(mgr.pending()) - len(items)
            self._send_json(
                HTTPStatus.OK,
                {
                    "machine": mgr.machine,
                    "project": mgr.project_name,
                    "approvals": items,
                    "local_only_pending": local_only,
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "approvals"] and parts[3] == "answer":
            if not self._authorized():
                return
            body = self._read_json()
            if body is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_body"})
                return
            ua = (self.headers.get("User-Agent") or "")[:80]
            try:
                result = self.channel.manager.answer(
                    parts[2],
                    str(body.get("answer", "")),
                    via="web",
                    by=f"{self._client()} {ua}".strip(),
                    decision_token=str(body.get("decision_token", "")) or None,
                )
            except ApprovalError as exc:
                self._send_json(exc.http_status, {"error": exc.code, "message": str(exc)})
                return
            self._send_json(
                HTTPStatus.OK, {"approval_id": result.approval_id, "status": "answered"}
            )
            return
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "approvals"
            and parts[3] == "decision"
        ):
            if not self._authorized():
                return
            body = self._read_json()
            if body is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_body"})
                return
            approval_id = parts[2]
            ua = (self.headers.get("User-Agent") or "")[:80]
            try:
                result = self.channel.manager.decide(
                    approval_id,
                    str(body.get("decision", "")),
                    str(body.get("scope", "once")),
                    via="web",
                    by=f"{self._client()} {ua}".strip(),
                    decision_token=str(body.get("decision_token", "")) or None,
                    fingerprint=str(body.get("fingerprint", "")) or None,
                )
            except ApprovalError as exc:
                self._send_json(exc.http_status, {"error": exc.code, "message": str(exc)})
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "approval_id": result.approval_id,
                    "status": result.status.value,
                    "scope": result.scope.value if result.scope else None,
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
