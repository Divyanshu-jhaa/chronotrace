"""Health and readiness check for MCP server.

Distinguishes process liveness from dependency readiness by verifying
Neo4j connectivity via the shared driver. Consumes validated ChronTraceConfig
only via explicit caller injection, never re-parses YAML/env.

Tool calls when not ready return graph_unavailable rather than crashing
or reporting success.

Decision 10: health check -> {status: ok|degraded, neo4j: ok|unavailable, analytics: ok}
"""

from __future__ import annotations

from typing import Any


def check(driver: Any | None = None) -> dict[str, str]:
    """Check graph connectivity and return health envelope.

    Args:
        driver: Graph driver/gateway exposing verify_connectivity().
                If None, returns degraded.

    Returns:
        dict with keys status (ok|degraded), neo4j (ok|unavailable), analytics (ok)
    """
    if driver is None:
        return {"status": "degraded", "neo4j": "unavailable", "analytics": "ok"}
    try:
        # Use verify_connectivity if available; otherwise treat as ok if callable
        verify = getattr(driver, "verify_connectivity", None)
        if callable(verify):
            verify()
        else:
            # Fallback: try driver as gateway with inner driver
            inner = getattr(driver, "driver", None)
            if inner is not None and hasattr(inner, "verify_connectivity"):
                inner.verify_connectivity()  # type: ignore
            else:
                # No verification method -> degraded but not crash
                return {"status": "degraded", "neo4j": "unavailable", "analytics": "ok"}
        return {"status": "ok", "neo4j": "ok", "analytics": "ok"}
    except Exception:
        return {"status": "degraded", "neo4j": "unavailable", "analytics": "ok"}


async def check_async(driver: Any | None = None) -> dict[str, str]:
    """Async wrapper for check (for consistency with async callers)."""
    return check(driver)


def is_ready(driver: Any | None = None) -> bool:
    """Return True if driver is ready (check returns ok)."""
    result = check(driver)
    return result.get("status") == "ok" and result.get("neo4j") == "ok"


__all__ = ["check", "check_async", "is_ready"]
