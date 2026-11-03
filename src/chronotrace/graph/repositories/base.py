"""Base helpers for repositories."""

from __future__ import annotations

from typing import Any

from ..errors import GraphConnectionError, GraphWriteError


def _sanitize(msg: str) -> str:
    if "password" in msg.lower() or "secret" in msg.lower():
        return "[Graph error — details redacted]"
    return msg


def map_neo4j_error(e: Exception) -> Exception:
    """Map Neo4j driver exceptions to domain errors without leaking internals."""
    # Already domain errors — pass through
    from ..errors import GraphError

    if isinstance(e, GraphError):
        return e

    msg = _sanitize(str(e)) if str(e) else e.__class__.__name__

    # Lazy import neo4j exceptions
    try:
        from neo4j.exceptions import (
            ConstraintError,
            CypherSyntaxError,
            Neo4jError,
            ServiceUnavailable,
            SessionExpired,
            TransientError,
        )

        if isinstance(e, ServiceUnavailable) or isinstance(e, SessionExpired):
            return GraphConnectionError(f"Neo4j unavailable: {msg}")
        if isinstance(e, ConstraintError):
            return GraphWriteError(f"Graph constraint violation: {msg}")
        if isinstance(e, CypherSyntaxError):
            return GraphWriteError(f"Graph query error: {msg}")
        if isinstance(e, TransientError):
            return GraphWriteError(f"Transient graph error: {msg}")
        if isinstance(e, Neo4jError):
            return GraphWriteError(f"Graph operation failed: {msg}")
    except ImportError:
        pass

    # Fallback based on message heuristics
    lower = msg.lower()
    if "unavailable" in lower or "connection" in lower or "refused" in lower:
        return GraphConnectionError(f"Neo4j unavailable: {msg}")
    if "constraint" in lower:
        return GraphWriteError(f"Graph constraint violation: {msg}")
    return GraphWriteError(f"Graph operation failed: {msg}")


def _execute_write(driver: Any, work: Any, *, is_read: bool = False) -> Any:
    """Execute a transactional function with proper session discipline.

    Uses ``with driver.session(database=...) as session:`` and
    ``session.execute_write`` / ``execute_read``. Falls back to direct
    ``session.run`` for test doubles that don't implement execute_*.
    Never holds a session as instance state.
    """
    try:
        with driver.session(database=driver.database) as session:
            try:
                if is_read:
                    return session.execute_read(work)
                else:
                    return session.execute_write(work)
            except AttributeError:
                # Mock driver without execute_* support — emulate transaction
                # by calling work with session as tx-like object
                # For mocks, work expects a tx with .run; session has .run.
                return work(session)
    except Exception as e:
        # Domain errors pass through
        from ..errors import GraphError

        if isinstance(e, GraphError):
            raise
        mapped = map_neo4j_error(e)
        # Preserve GraphConnectionError vs Write vs Read mapping
        raise mapped from e


def chunked(iterable: list[Any], size: int) -> Any:  # type: ignore[misc]
    """Yield successive chunks of given size."""
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]
