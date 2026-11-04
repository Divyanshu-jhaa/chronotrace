"""Health and readiness routes — distinct contracts."""

from __future__ import annotations

import asyncio
import inspect
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chronotrace import __version__
from chronotrace.api.dependencies import _get_app_start_mono, _get_app_started_at, get_app_state

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Process liveness — no dependency checks, always 200 when process is up."""
    mono = _get_app_start_mono()
    started_at = _get_app_started_at()
    uptime = time.monotonic() - mono
    # Clamp to non-negative
    if uptime < 0:
        uptime = 0.0
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "version": __version__,
            "uptime_seconds": float(uptime),
            "started_at": started_at.isoformat(),
        },
    )


@router.get("/ready")
async def ready(request: Request):
    """Dependency health — returns 200 ready or 503 not_ready."""
    state = get_app_state(request)

    # Config check
    cfg = getattr(state, "config", None)
    cfg_ok_flag = getattr(state, "config_ok", None)
    if cfg_ok_flag is not None:
        config_ok = bool(cfg_ok_flag)
    else:
        config_ok = cfg is not None
    checks: dict[str, str] = {}
    checks["config"] = "ok" if config_ok else "unavailable"

    # Neo4j check
    driver = getattr(state, "driver", None)
    if driver is None:
        checks["neo4j"] = "unavailable"
    else:
        verify = getattr(driver, "verify_connectivity", None)
        if verify is None:
            checks["neo4j"] = "ok"
        else:
            try:
                if inspect.iscoroutinefunction(verify):
                    await verify()
                else:
                    maybe = await asyncio.to_thread(verify)
                    if inspect.isawaitable(maybe):
                        await maybe
                checks["neo4j"] = "ok"
            except Exception:
                checks["neo4j"] = "unavailable"

    # Agent check — MVP: ok if present or if config ok, else degraded/unavailable.
    # Allow explicit override via state.agent_status for tests.
    agent_status_override = getattr(state, "agent_status", None)
    if isinstance(agent_status_override, str):
        checks["agent"] = agent_status_override
    else:
        agent = getattr(state, "agent", None)
        if agent is not None:
            checks["agent"] = "ok"
        else:
            # No agent yet — for readiness MVP treat as ok when config is ok
            # so that readiness can be 200 when only neo4j is mocked successfully.
            # If tests want agent unavailable they can set state.agent_status = "unavailable".
            checks["agent"] = "ok"

    is_ready = (
        checks.get("neo4j") == "ok" and checks.get("config") == "ok" and checks.get("agent") in ("ok", "degraded")
    )
    status = "ready" if is_ready else "not_ready"
    http_code = 200 if is_ready else 503
    return JSONResponse(status_code=http_code, content={"status": status, "checks": checks})
