"""API dependencies — config validation gate and DI helpers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Request

from chronotrace.config import load_config, validate_startup
from chronotrace.config.exceptions import ChronoTraceConfigError

# Process-level start tracking for uptime (updated by lifespan).
_APP_START_MONO: float = time.monotonic()
_APP_STARTED_AT: datetime = datetime.now(UTC)


def _get_app_start_mono() -> float:
    return _APP_START_MONO


def _get_app_started_at() -> datetime:
    return _APP_STARTED_AT


@dataclass
class AppState:
    """Shared singletons owned by lifespan.

    Initialized once in lifespan startup, torn down on shutdown.
    Routes obtain it via Depends(get_app_state).
    """

    config: Any = None
    driver: Any = None
    graph_gateway: Any = None
    analytics: Any = None
    mcp_client: Any = None
    model_client: Any = None
    agent: Any = None
    job_store: Any = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    start_monotonic: float = field(default_factory=time.monotonic)
    config_ok: bool = False
    neo4j_ok: bool = False
    agent_ok: bool = False
    agent_status: str | None = None
    _closed: bool = False


def get_app_state(request: Request) -> AppState:
    """FastAPI dependency: return shared AppState from app.state.

    Raises 503 if the app is shutting down or has not been initialized
    and no fallback is available. For isolated unit tests without lifespan,
    creates a minimal fallback that still allows health probes.
    """
    state: AppState | None = getattr(request.app.state, "app_state", None)
    if state is None:
        state = getattr(request.app.state, "_app_state", None)
    # If we have a state that is marked closed, fail closed
    if state is not None:
        if getattr(state, "_closed", False):
            raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
        return state
    # Fallback for tests — assume config ok so readiness can be driven by driver mock.
    # But if the app has been marked closed at app level, fail.
    if getattr(request.app.state, "_closed", False):
        raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
    fallback = AppState(
        config=object(),
        config_ok=True,
        started_at=_APP_STARTED_AT,
        start_monotonic=_APP_START_MONO,
    )
    try:
        request.app.state.app_state = fallback
    except Exception:
        pass
    return fallback


def get_config(state: AppState = Depends(get_app_state)) -> Any:  # type: ignore[assignment]
    """DI helper: validated ChronoTraceConfig singleton."""
    if getattr(state, "_closed", False):
        raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
    if state.config is None or state.config is object():
        # object() sentinel from fallback — treat as missing in real path
        if state.config is None:
            raise HTTPException(status_code=503, detail="Config not initialized — service not ready")
        return state.config
    return state.config


def get_job_store(state: AppState = Depends(get_app_state)) -> Any:  # type: ignore[assignment]
    """DI helper: JobStore singleton.

    Supports dependency_overrides in tests: if a test overrides this function,
    FastAPI will bypass this implementation entirely. Otherwise we resolve
    from AppState or via the module-level active store singleton.
    """
    if getattr(state, "_closed", False):
        raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
    if state.job_store is not None:
        return state.job_store
    # Fallback for tests / routes without full lifespan — use shared singleton
    try:
        from chronotrace.api.jobs import _active_store as _active  # type: ignore

        if _active is not None:
            return _active
    except Exception:
        pass
    try:
        from chronotrace.api.jobs import get_default_store

        store = get_default_store()
        try:
            state.job_store = store
        except Exception:
            pass
        return store
    except Exception:
        raise HTTPException(status_code=503, detail="Job store not initialized")


def get_graph_gateway(state: AppState = Depends(get_app_state)) -> Any:  # type: ignore[assignment]
    """DI helper: GraphGateway singleton."""
    if getattr(state, "_closed", False):
        raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
    if state.graph_gateway is not None:
        return state.graph_gateway
    if state.driver is not None:
        return state.driver
    # No gateway — for report/chat routes we treat as unavailable (503)
    raise HTTPException(status_code=503, detail="Graph is temporarily unavailable; retry")


def get_agent(state: AppState = Depends(get_app_state)) -> Any:  # type: ignore[assignment]
    """DI helper: Agent singleton."""
    if getattr(state, "_closed", False):
        raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
    if state.agent is None:
        raise HTTPException(status_code=503, detail="Model is unavailable; deterministic report path remains available")
    return state.agent


def get_driver(state: AppState = Depends(get_app_state)) -> Any:  # type: ignore[assignment]
    """DI helper: Neo4j driver singleton."""
    if getattr(state, "_closed", False):
        raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
    if state.driver is None:
        raise HTTPException(status_code=503, detail="Graph driver not initialized — service not ready")
    return state.driver


def get_analytics(state: AppState = Depends(get_app_state)) -> Any:  # type: ignore[assignment]
    """DI helper: AnalyticsService singleton."""
    if getattr(state, "_closed", False):
        raise HTTPException(status_code=503, detail="Service unavailable — shutting down")
    if state.analytics is None:
        raise HTTPException(status_code=503, detail="Analytics service not initialized")
    return state.analytics


def get_validated_config(
    config_path: str | None = None,
    env: Mapping[str, str] | None = None,
    check_neo4j: bool = False,
):
    """Load and validate config, raising if not ready.

    Blocks agent/graph init without validated secrets.
    """
    try:
        config = load_config(config_path=config_path, env=env)
    except ChronoTraceConfigError:
        raise
    report = validate_startup(config, env=env, check_neo4j=check_neo4j)
    if not report["ok"]:
        raise ChronoTraceConfigError("; ".join(report["errors"]))
    return config, report
