"""Router FastAPI para recibir telemetría validada desde Tasker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .fuzzy_logic import FuzzyEngine, TelemetryPayload


def build_telemetry_router(engine: FuzzyEngine) -> APIRouter:
    """Construye el router de Tasker sin duplicar la autenticación global."""
    router = APIRouter()

    @router.post("/api/telemetry/tasker")
    async def receive_tasker_telemetry(payload: TelemetryPayload) -> dict[str, Any]:
        """Valida y entrega un heartbeat al motor de telemetría."""
        return await engine.ingest(payload)

    return router


__all__ = ["TelemetryPayload", "build_telemetry_router"]
