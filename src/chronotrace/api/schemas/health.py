"""Health and readiness response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """GET /health response — process liveness only, no dependency checks."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    version: str
    uptime_seconds: float
    started_at: str | None = None


class ReadyChecks(BaseModel):
    """Per-dependency health details."""

    model_config = ConfigDict(extra="forbid")

    neo4j: Literal["ok", "unavailable"]
    config: Literal["ok", "unavailable"]
    agent: Literal["ok", "degraded", "unavailable", "max_unavailable"] = "ok"


class ReadyResponse(BaseModel):
    """GET /ready response — dependency health."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"]
    checks: ReadyChecks


__all__ = ["HealthResponse", "ReadyChecks", "ReadyResponse"]
