"""A model caller for work outside an agent session (engine jobs, reviews) whose spend still
lands in the session store under its own session, so the cost ledger stays fully loaded."""

from __future__ import annotations

from typing import Any


class RecordedCaller:
    def __init__(self, config, role: str, project_path: str, model: str | None = None) -> None:
        from trendlab.config.loader import trendlab_home
        from trendlab.providers.gateway import ModelGateway
        from trendlab.providers.registry import resolve_role
        from trendlab.sessions.store import SessionStore
        from trendlab.telemetry.costs import CostTracker
        from trendlab.telemetry.events import EventBus

        self.ref = model or resolve_role(config, role, config.defaults.model)
        self.role = role
        self.gateway = ModelGateway(config, EventBus(), role)
        self.costs = CostTracker(config)
        self.store = SessionStore(trendlab_home() / "sessions.db")
        import socket

        self.session_id = self.store.create_session(
            project_path, socket.gethostname(), self.ref, label=role
        )
        self.costs.on_record = self._persist

    def _persist(self, rec) -> None:
        self.store.record_model_call(
            self.session_id,
            rec.model_ref,
            rec.role,
            rec.input_tokens,
            rec.output_tokens,
            rec.cached_input_tokens,
            rec.latency_ms,
            rec.cost_usd,
        )

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        response, used = await self.gateway.complete(self.ref, messages, None, role=self.role)
        self.costs.record(used, response.usage, 0, role=self.role)
        return response.text

    @property
    def cost(self) -> float:
        return self.costs.total_usd

    async def close(self) -> None:
        try:
            await self.gateway.close()
        except Exception:  # noqa: BLE001
            pass
        self.store.close()
