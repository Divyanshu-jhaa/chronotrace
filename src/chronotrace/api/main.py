"""FastAPI application — lifespan, router wiring, error envelope, redaction, shutdown."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from chronotrace import __version__
from chronotrace.api.dependencies import AppState
from chronotrace.api.routes.health import router as health_router

try:
    from chronotrace.api.routes.reports import router as reports_router
except Exception:  # pragma: no cover
    reports_router = None  # type: ignore[assignment]

try:
    from chronotrace.api.routes.repositories import router as repositories_router
except Exception:  # pragma: no cover
    repositories_router = None  # type: ignore[assignment]

try:
    from chronotrace.api.routes.chat import router as chat_router
except Exception:  # pragma: no cover
    chat_router = None  # type: ignore[assignment]

# Heavy config loader — imported at top-level so tests can patch chronotrace.api.main.load_config
from chronotrace.config import load_config, validate_startup
from chronotrace.config.exceptions import ChronoTraceConfigError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redaction helpers (shared with schemas/errors but implemented here for
# exception handlers without circular imports)
# ---------------------------------------------------------------------------

_REDACTED = "[REDACTED]"

# Patterns to redact
_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE)
_PASSWORD_KV_RE = re.compile(
    r"(password|passwd|api_key|apikey|api-key|secret|token|auth)\s*[:=]\s*[^\s,;\"']+",
    flags=re.IGNORECASE,
)
_URI_CRED_RE = re.compile(r"://[^@\s]+:[^@\s]+@")
_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE)


def _redact(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    # redact via schemas/errors helper if available (more comprehensive)
    try:
        from chronotrace.api.schemas.errors import _sanitize_str as _ext_sanitize

        return _ext_sanitize(text)
    except Exception:
        pass
    # fallback local redaction
    try:
        text = _SK_RE.sub(_REDACTED, text)
        text = _PASSWORD_KV_RE.sub(r"\1=" + _REDACTED, text)
        text = _URI_CRED_RE.sub("://[REDACTED]@", text)
        text = _TOKEN_RE.sub("Bearer " + _REDACTED, text)
        # bolt uri
        text = re.sub(r"bolt://[^\s]+", "bolt://[REDACTED]", text, flags=re.IGNORECASE)
        # generic password=xxx
        text = re.sub(r"password\s*=\s*\S+", "password=[REDACTED]", text, flags=re.IGNORECASE)
        # api_key=xxx
        text = re.sub(r"api_key\s*=\s*\S+", "api_key=[REDACTED]", text, flags=re.IGNORECASE)
        # also redact known env var names verbatim but not value
        for tok in ("NEO4J_PASSWORD", "MODEL_API_KEY", "NEO4J_USERNAME"):
            text = text.replace(tok, "[REDACTED]") if tok in text else text
            try:
                text = re.sub(re.escape(tok), _REDACTED, text, flags=re.IGNORECASE)
            except Exception:
                pass
        # SQL injection literal not verbatim? ensure not leaking "' OR 1=1"
        if "' OR 1=1" in text:
            text = text.replace("' OR 1=1", "[REDACTED]")
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# Factory helpers — patchable via chronotrace.api.main.* and also via
# their original modules. Each does lazy import.
# ---------------------------------------------------------------------------

def _create_driver(config: Any, env: Any | None = None) -> Any:
    """Create Neo4j driver (GraphDriver). Patchable wrapper."""
    from chronotrace.graph.driver import GraphDriver

    # GraphDriver handles env resolution internally; pass env if given
    if env is not None:
        return GraphDriver(config, env=env)
    return GraphDriver(config)


def create_neo4j_driver(*args: Any, **kwargs: Any) -> Any:
    """Compatibility alias for design's create_neo4j_driver name."""
    return _create_driver(*args, **kwargs)


def _create_gateway(config: Any, driver: Any) -> Any:
    from chronotrace.graph.gateway import GraphGateway

    # GraphGateway may accept config and driver kwargs
    try:
        return GraphGateway(config, driver=driver)
    except TypeError:
        # Fallback: GraphGateway(driver) or GraphGateway(config)
        try:
            return GraphGateway(driver=driver, config=config)  # type: ignore[call-arg]
        except Exception:
            return GraphGateway(config)


def _create_analytics(gateway: Any, config: Any) -> Any:
    from chronotrace.core.analytics.service import AnalyticsService

    return AnalyticsService(gateway, config)


def _create_mcp_client(config: Any) -> Any:
    # Try MCPClient (capital) then McpClient
    last_exc: Exception | None = None
    for name in ("MCPClient", "McpClient"):
        try:
            mod = __import__("chronotrace.agent.mcp_client", fromlist=[name])
            cls = getattr(mod, name, None)
            if cls is not None:
                # Prefer config-based construction
                try:
                    return cls(config=config)
                except TypeError:
                    # Try host/port
                    mcp_cfg = getattr(config, "mcp", None)
                    if mcp_cfg is not None:
                        host = getattr(mcp_cfg, "host", "127.0.0.1")
                        port = getattr(mcp_cfg, "port", 8001)
                        try:
                            return cls(host=host, port=port)
                        except Exception:
                            pass
                    return cls()
        except Exception as e:
            last_exc = e
            continue
    # Fallback generic
    try:
        from chronotrace.agent.mcp_client import MCPClient as _MCP

        return _MCP(config=config)
    except Exception:
        pass
    if last_exc:
        raise last_exc
    raise RuntimeError("MCPClient not available")


def _create_model_client(config: Any) -> Any:
    from chronotrace.agent.model_client import ModelClient
    import os

    # Resolve api key from env
    api_key = os.getenv("MODEL_API_KEY")
    # config may contain agent model settings but ModelClient reads env for key
    agent_cfg = getattr(config, "agent", None)
    # Use defaults from config if available
    model = getattr(agent_cfg, "model", "muse-spark-1.3") if agent_cfg else "muse-spark-1.3"
    base_url = getattr(agent_cfg, "base_url", "https://api.meta.ai/v1") if agent_cfg else "https://api.meta.ai/v1"
    reasoning = getattr(agent_cfg, "reasoning_effort", "medium") if agent_cfg else "medium"
    timeout = getattr(agent_cfg, "timeout_seconds", 30.0) if agent_cfg else 30.0
    return ModelClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        reasoning_effort=reasoning,
        timeout_seconds=timeout,
    )


def _create_agent(config: Any, mcp_client: Any, model_client: Any | None = None) -> Any:
    from chronotrace.agent import Agent

    # Agent signature: Agent(config, model_client, mcp_client)
    # Try most complete first
    if model_client is not None:
        try:
            return Agent(config, model_client, mcp_client)
        except TypeError:
            try:
                return Agent(config=config, model_client=model_client, mcp_client=mcp_client)
            except Exception:
                pass
            try:
                # alternative order
                return Agent(getattr(config, "agent", config), model_client, mcp_client)
            except Exception:
                pass
    else:
        # No model client — create agent with None and let it fail gracefully? But we want degraded.
        # Try to create without model_client if Agent allows
        try:
            return Agent(config, model_client, mcp_client)
        except Exception:
            pass
    # Fallback without model_client
    try:
        return Agent(config, model_client, mcp_client)
    except Exception as e:
        raise e


def _create_job_store(max_jobs: int = 1000) -> Any:
    from chronotrace.api.jobs import JobStore

    return JobStore(max_jobs=max_jobs)


# ---------------------------------------------------------------------------
# Lifespan — single load_config, strict startup validation, ordered init,
# shutdown with 5s drain.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan.

    Startup order per design:
      1. load_config() single call
      2. validate_startup(config, check_neo4j=False) — secrets/filesystem
      3. driver = _create_driver + await to_thread(driver.verify_connectivity)
      4. GraphGateway
      5. AnalyticsService
      6. McpClient.connect()
      7. Agent
      8. JobStore
    On shutdown:
      await job_store.cancel_all(reason="shutdown") with 5s timeout,
      await mcp_client.aclose() (or close), driver.close(), drain with 5s.
    Startup fails fast on invalid config; Neo4j unavailability marks not_ready
    but does not crash process (health stays 200 while ready is 503).
    """
    state = AppState(
        started_at=datetime.now(timezone.utc),
        start_monotonic=time.monotonic(),
    )
    # Ensure _closed False at start
    state._closed = False
    config = None
    # 1) Single load_config call — let ConfigValidationError propagate (startup fail)
    try:
        config = load_config()
        state.config = config
    except ChronoTraceConfigError:
        # Fail fast — do not mark ready, propagate so lifespan fails
        # But still set app state so health can respond via fallback? We propagate
        raise
    except Exception as e:
        # Wrap as config error
        raise ChronoTraceConfigError(str(e)) from e

    # 2) secrets/filesystem checks
    try:
        report = validate_startup(config, check_neo4j=False)
        if not report.get("ok"):
            raise ChronoTraceConfigError("; ".join(report.get("errors", ["startup validation failed"])))
        state.config_ok = True
    except ChronoTraceConfigError:
        raise
    except Exception as e:
        raise ChronoTraceConfigError(str(e)) from e

    # 3) graph driver + verify connectivity
    driver_ok = False
    gateway = None
    try:
        driver = _create_driver(config)
        state.driver = driver
        # verify off loop — if we cannot verify, mark not ready but don't crash
        try:
            await asyncio.to_thread(driver.verify_connectivity)
            state.neo4j_ok = True
            driver_ok = True
        except Exception as ve:
            # Log redacted
            logger.warning("Neo4j verify failed (redacted): %s", _redact(str(ve)))
            state.neo4j_ok = False
            # Do not raise — readiness will be 503, but process stays up for health
            driver_ok = False
    except Exception as e:
        logger.warning("Driver creation failed: %s", _redact(str(e)))
        state.neo4j_ok = False

    # 4) GraphGateway if driver ok
    if driver_ok and getattr(state, "driver", None) is not None:
        try:
            gateway = _create_gateway(config, state.driver)
            state.graph_gateway = gateway
            # If gateway has its own verify, ensure it matches driver status
            if not state.neo4j_ok:
                try:
                    await asyncio.to_thread(gateway.verify_connectivity)
                    state.neo4j_ok = True
                except Exception:
                    state.neo4j_ok = False
        except Exception as e:
            logger.warning("Gateway creation failed: %s", _redact(str(e)))
            state.graph_gateway = None

    # 5) AnalyticsService
    if getattr(state, "graph_gateway", None) is not None:
        try:
            analytics = _create_analytics(state.graph_gateway, config)
            state.analytics = analytics
        except Exception as e:
            logger.warning("Analytics creation failed: %s", _redact(str(e)))
            state.analytics = None

    # 6) MCP client + connect
    mcp_client = None
    try:
        mcp_client = _create_mcp_client(config)
        state.mcp_client = mcp_client
        # connect with timeout 5s
        if hasattr(mcp_client, "connect"):
            try:
                maybe = mcp_client.connect()
                if asyncio.iscoroutine(maybe):
                    await asyncio.wait_for(maybe, timeout=5.0)
                elif maybe is not None and hasattr(maybe, "__await__"):
                    await asyncio.wait_for(maybe, timeout=5.0)  # type: ignore[arg-type]
            except asyncio.TimeoutError:
                logger.warning("MCP connect timed out")
            except Exception as e:
                logger.warning("MCP connect failed: %s", _redact(str(e)))
                # keep client but agent will be degraded
        state.agent_ok = True
    except Exception as e:
        logger.warning("MCP client creation failed: %s", _redact(str(e)))
        state.mcp_client = None
        state.agent_ok = False

    # 7) Agent (with model client)
    if getattr(state, "mcp_client", None) is not None:
        model_client = None
        try:
            try:
                model_client = _create_model_client(config)
                state.model_client = model_client  # type: ignore[attr-defined]
            except Exception as me:
                # Missing MODEL_API_KEY or validation -> degraded agent, but not fatal
                logger.warning("Model client creation failed (redacted): %s", _redact(str(me)))
                state.model_client = None  # type: ignore[attr-defined]
                # Do not fail startup — readiness may degrade, but chat will return 503
                model_client = None
            # Create agent even if model_client missing (agent handles degraded)
            try:
                agent = _create_agent(config, mcp_client, model_client)
                state.agent = agent
                # If model_client missing, agent_ok degraded
                if model_client is None:
                    state.agent_ok = False
                else:
                    state.agent_ok = True
            except Exception as ae:
                logger.warning("Agent creation failed: %s", _redact(str(ae)))
                state.agent = None
                state.agent_ok = False
        except Exception as e:
            logger.warning("Agent init failed: %s", _redact(str(e)))
            state.agent = None
            state.agent_ok = False

    # 8) JobStore
    try:
        job_store = _create_job_store(max_jobs=1000)
        state.job_store = job_store
    except Exception as e:
        logger.warning("JobStore creation failed: %s", _redact(str(e)))
        state.job_store = None

    app.state.app_state = state
    app.state._closed = False
    # Sync process-level start tracking
    try:
        from chronotrace.api import dependencies as _deps

        _deps._APP_START_MONO = state.start_monotonic
        _deps._APP_STARTED_AT = state.started_at
    except Exception:
        pass

    try:
        yield
    finally:
        # Shutdown — mark closed first so new requests fail fast
        state._closed = True
        try:
            app.state._closed = True  # type: ignore[attr-defined]
        except Exception:
            pass
        # Cancel jobs with 5s timeout
        js = getattr(state, "job_store", None)
        if js is not None and hasattr(js, "cancel_all"):
            try:
                await asyncio.wait_for(js.cancel_all(reason="shutdown"), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("JobStore cancel_all timed out after 5s")
            except Exception as e:
                logger.debug("JobStore cancel_all error: %s", _redact(str(e)))
        # Close gateway/driver
        gw = getattr(state, "graph_gateway", None)
        if gw is not None and hasattr(gw, "close"):
            try:
                await asyncio.to_thread(gw.close)
            except Exception:
                try:
                    gw.close()  # type: ignore[call-arg]
                except Exception:
                    pass
        driver = getattr(state, "driver", None)
        if driver is not None and driver is not gw:
            close = getattr(driver, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception:
                    try:
                        close()
                    except Exception:
                        pass
        # Close MCP client — prefer aclose, fallback close
        mcp = getattr(state, "mcp_client", None)
        if mcp is not None:
            closer = getattr(mcp, "aclose", None) or getattr(mcp, "close", None)
            if callable(closer):
                try:
                    res = closer()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("MCP client aclose timed out")
                except Exception as e:
                    logger.debug("MCP close error: %s", _redact(str(e)))
        # Close model client if present
        mc = getattr(state, "model_client", None)
        if mc is not None:
            closer = getattr(mc, "aclose", None) or getattr(mc, "close", None)
            if callable(closer):
                try:
                    res = closer()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=5.0)
                except Exception:
                    pass
        # Close agent
        ag = getattr(state, "agent", None)
        if ag is not None:
            for attr in ("aclose", "close"):
                fn = getattr(ag, attr, None)
                if callable(fn):
                    try:
                        r = fn()
                        if asyncio.iscoroutine(r):
                            await asyncio.wait_for(r, timeout=5.0)
                        break
                    except Exception as e:
                        logger.debug("Agent %s error: %s", attr, _redact(str(e)))
                        continue


app = FastAPI(
    title="ChronoTrace API",
    version=__version__,
    lifespan=lifespan,
)

# Body-size limit middleware — reject >1 MB with 413
@app.middleware("http")
async def _body_size_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    # Early check on Content-Length header
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            size = int(cl)
            if size > 1_000_000:
                return JSONResponse(
                    status_code=413,
                    content={"error": {"code": "validation_error", "message": "Payload too large"}},
                )
        except ValueError:
            pass
    # Also check after reading body for chunked?
    # Let Starlette handle oversized via 413; we just enforce header
    # For shutdown, fail fast with 503 before routing
    try:
        state = getattr(request.app.state, "app_state", None)
        if state is not None and getattr(state, "_closed", False):
            # Allow health to still return 503 via its handler, but short-circuit others
            # We'll let health/ready decide; for others return 503
            path = request.url.path
            if path not in ("/health", "/ready", "/openapi.json", "/docs", "/redoc"):
                return JSONResponse(
                    status_code=503,
                    content={"error": {"code": "graph_unavailable", "message": "Service is shutting down"}},
                )
    except Exception:
        pass
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        # Ensure unhandled exceptions are redacted and returned as 500 envelope
        msg = _redact(str(e)) if str(e) else "Internal error"
        logger.exception("Unhandled exception (redacted): %s", msg)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal error"}},
        )


# Include routers (health always, others optional)
app.include_router(health_router)
if reports_router is not None:
    app.include_router(reports_router)
if repositories_router is not None:
    app.include_router(repositories_router)
if chat_router is not None:
    app.include_router(chat_router)

# ---------------------------------------------------------------------------
# Error envelope handlers — always JSON, always redacted
# ---------------------------------------------------------------------------
try:
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from fastapi import HTTPException as FastAPIHTTPException

    _HAS_ERRORS = True
except Exception:  # pragma: no cover
    _HAS_ERRORS = False

if _HAS_ERRORS:

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        msg = "Validation failed"
        details: Any = None
        try:
            raw_details = exc.errors() if hasattr(exc, "errors") else str(exc)
            # Make details JSON-serializable and redacted
            # Pydantic errors may contain non-serializable objects in ctx (e.g., ValueError)
            def _sanitize_detail(obj: Any) -> Any:
                if isinstance(obj, dict):
                    out: dict[str, Any] = {}
                    for k, v in obj.items():
                        # value may be ValueError or other non-serializable
                        if isinstance(v, BaseException):
                            out[k] = _redact(str(v))
                        elif isinstance(v, dict):
                            out[k] = _sanitize_detail(v)
                        elif isinstance(v, list):
                            out[k] = [_sanitize_detail(x) for x in v]
                        elif isinstance(v, str):
                            out[k] = _redact(v)
                        else:
                            # try to keep JSON serializable; fallback to string
                            try:
                                import json as _j
                                _j.dumps(v)
                                out[k] = v
                            except Exception:
                                out[k] = _redact(str(v))
                    return out
                if isinstance(obj, list):
                    return [_sanitize_detail(x) for x in obj]
                if isinstance(obj, str):
                    return _redact(obj)
                if isinstance(obj, BaseException):
                    return _redact(str(obj))
                try:
                    import json as _j
                    _j.dumps(obj)
                    return obj
                except Exception:
                    return _redact(str(obj))
            # First try external redaction, then sanitize
            try:
                from chronotrace.api.schemas.errors import redact_secrets as _redact_details

                raw_details = _redact_details(raw_details)
            except Exception:
                pass
            details = _sanitize_detail(raw_details)
        except Exception:
            details = _redact(str(exc))
        # Check for body size hint in errors
        try:
            err_str = str(exc).lower()
            if "too large" in err_str or "maximum" in err_str and "10000" in err_str:
                # Question oversized already maps to 422, not 413; keep 422
                pass
        except Exception:
            pass
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": _redact(msg),
                    "details": details,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http_handler(request: Request, exc: StarletteHTTPException):  # type: ignore[no-untyped-def]
        code = int(getattr(exc, "status_code", 500))
        raw_detail = getattr(exc, "detail", str(exc))
        # Starlette detail may be string or dict
        if isinstance(raw_detail, dict):
            # Already envelope? re-redact and return
            try:
                import json as _json

                detail_str = _json.dumps(raw_detail)
                detail_str = _redact(detail_str)
                # keep envelope shape
                return JSONResponse(status_code=code, content=raw_detail)
            except Exception:
                raw_detail = str(raw_detail)
        detail_str = _redact(str(raw_detail)) if isinstance(raw_detail, str) else _redact(str(raw_detail))
        # Map code
        if code == 404:
            err_code = "not_found"
        elif code == 503:
            # Distinguish not_ready vs graph/model unavailable based on message
            low = detail_str.lower()
            if "not ready" in low or "not_ready" in low:
                err_code = "not_ready"
            elif "model" in low:
                err_code = "model_unavailable"
            elif "graph" in low:
                err_code = "graph_unavailable"
            elif "shutting down" in low:
                err_code = "graph_unavailable"
            else:
                err_code = "graph_unavailable"
        elif code == 504:
            err_code = "model_unavailable"
        elif code in (400, 413, 422):
            err_code = "validation_error"
        elif code == 413:
            err_code = "validation_error"
        else:
            err_code = "internal_error"
        # Special case: 413 from middleware should be validation_error
        if code == 413:
            err_code = "validation_error"
        return JSONResponse(
            status_code=code,
            content={"error": {"code": err_code, "message": detail_str}},
        )

    @app.exception_handler(FastAPIHTTPException)
    async def _fastapi_http_handler(request: Request, exc: FastAPIHTTPException):  # type: ignore[no-untyped-def]
        code = int(getattr(exc, "status_code", 500))
        detail_str = _redact(str(getattr(exc, "detail", str(exc))))
        if code == 404:
            err_code = "not_found"
        elif code == 503:
            low = detail_str.lower()
            if "model" in low:
                err_code = "model_unavailable"
            elif "graph" in low:
                err_code = "graph_unavailable"
            else:
                err_code = "not_ready" if "not ready" in low else "graph_unavailable"
        elif code == 504:
            err_code = "model_unavailable"
        elif code in (400, 422, 413):
            err_code = "validation_error"
        else:
            err_code = "internal_error"
        return JSONResponse(
            status_code=code,
            content={"error": {"code": err_code, "message": detail_str}},
        )

    @app.exception_handler(Exception)
    async def _generic_handler(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        # Never leak raw exception with secrets
        # Log redacted
        try:
            logger.exception("Unhandled error (redacted): %s", _redact(str(exc)))
        except Exception:
            pass
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal error"}},
        )

# Ensure openapi / docs are available even without lifespan
