"""Structured error mapping for MCP tools.

Maps domain exceptions to MCP-compatible error envelopes
``{status, error: {code, message}, isError: true}`` without leaking
secrets, raw Cypher, or connection strings.

Secret redaction mirrors ``chronotrace.ingestion.exceptions._redact_secrets``.
"""

from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"://[^@\s]+:[^@\s]+@"), "://[REDACTED]@"),
    (re.compile(r"://[^@\s]+@"), "://[REDACTED]@"),
]

_SENSITIVE_TOKENS = ("password", "passwd", "secret", "token", "api_key", "api-key")
_CYPHER_KEYWORDS = ("MATCH", "MERGE", "DETACH DELETE", "OPTIONAL MATCH", "UNWIND", "CYPHER")
_CONNECTION_TOKENS = ("bolt://", "neo4j://", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_URI")


def _redact_secrets(msg: str) -> str:
    if not isinstance(msg, str):
        msg = str(msg)
    for pat, repl in _SECRET_PATTERNS:
        try:
            msg = pat.sub(repl, msg)
        except Exception:
            pass
    lower = msg.lower()
    for token in _SENSITIVE_TOKENS:
        if token in lower:
            try:
                msg = re.sub(
                    rf"{re.escape(token)}[\s=:]+[^\s,;]+",
                    f"{token}=[REDACTED]",
                    msg,
                    flags=re.IGNORECASE,
                )
            except Exception:
                pass
    return msg


def _sanitize_message(msg: str) -> str:
    safe = _redact_secrets(msg)
    upper = safe.upper()
    has_cypher = any(kw in upper for kw in _CYPHER_KEYWORDS)
    has_connection_leak = any(tok.lower() in safe.lower() for tok in _CONNECTION_TOKENS)
    if has_cypher:
        earliest = len(safe)
        earliest_kw: str | None = None
        for kw in _CYPHER_KEYWORDS:
            idx = upper.find(kw)
            if idx != -1 and idx < earliest:
                earliest = idx
                earliest_kw = kw
        if earliest_kw is not None and earliest < len(safe):
            prefix = safe[:earliest].strip().rstrip(" :;,-")
            if prefix:
                safe = f"{prefix} [query details redacted]"
            else:
                safe = "[query details redacted]"
    if has_connection_leak:
        for tok in _CONNECTION_TOKENS:
            if tok.lower() in safe.lower():
                try:
                    safe = re.sub(
                        rf"{re.escape(tok)}[\s=:]*[^\s,;]*",
                        f"{tok}=[REDACTED]",
                        safe,
                        flags=re.IGNORECASE,
                    )
                except Exception:
                    pass
        try:
            safe = re.sub(r"bolt://[^\s]+", "bolt://[REDACTED]", safe, flags=re.IGNORECASE)
            safe = re.sub(r"neo4j://[^\s]+", "neo4j://[REDACTED]", safe, flags=re.IGNORECASE)
        except Exception:
            pass
        safe = _redact_secrets(safe)
    return safe.strip()


class ValidationError(Exception):
    """Input validation failure -> validation_error status."""

    def __init__(self, message: str, code: str = "VALIDATION_ERROR") -> None:
        sanitized = _sanitize_message(message)
        super().__init__(sanitized)
        self.code = code
        self.message = sanitized


class GraphNotAvailable(Exception):
    """Graph driver / database unavailable -> graph_unavailable."""

    def __init__(
        self,
        message: str = "Graph is temporarily unavailable; retry",
        code: str = "GRAPH_UNAVAILABLE",
    ) -> None:
        sanitized = _sanitize_message(message)
        super().__init__(sanitized)
        self.code = code
        self.message = sanitized


class NotFound(Exception):
    """Repository or file not found -> not_found."""

    def __init__(self, message: str, code: str = "NOT_FOUND") -> None:
        sanitized = _sanitize_message(message)
        super().__init__(sanitized)
        self.code = code
        self.message = sanitized


GraphUnavailable = GraphNotAvailable
ToolValidationError = ValidationError


def to_error_envelope(
    status: str,
    code: str,
    message: str,
    *,
    is_error: bool = True,
) -> dict[str, Any]:
    safe_msg = _sanitize_message(message)
    safe_code = _sanitize_message(code)
    return {
        "status": status,
        "error": {"code": safe_code, "message": safe_msg},
        "isError": is_error,
    }


def validation_error_envelope(
    message: str, code: str = "VALIDATION_ERROR"
) -> dict[str, Any]:
    return to_error_envelope("validation_error", code, message, is_error=True)


def not_found_envelope(message: str, code: str = "NOT_FOUND") -> dict[str, Any]:
    return to_error_envelope("not_found", code, message, is_error=True)


def graph_unavailable_envelope(
    message: str = "Graph is temporarily unavailable; retry",
    code: str = "GRAPH_UNAVAILABLE",
) -> dict[str, Any]:
    return to_error_envelope("graph_unavailable", code, message, is_error=True)


def internal_error_envelope(
    message: str = "Internal error",
    code: str = "INTERNAL",
) -> dict[str, Any]:
    safe = "Internal error"
    return to_error_envelope("internal_error", code, safe, is_error=True)


def map_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, dict):
        try:
            status = str(exc.get("status", "internal_error"))
            err = exc.get("error", {})
            if isinstance(err, dict):
                code = str(err.get("code", "INTERNAL"))
                msg = str(err.get("message", "Internal error"))
            else:
                code = "INTERNAL"
                msg = str(err)
            is_err = bool(exc.get("isError", True))
            return to_error_envelope(status, code, msg, is_error=is_err)
        except Exception:
            return internal_error_envelope()
    if isinstance(exc, ValidationError):
        return to_error_envelope("validation_error", exc.code, exc.message, is_error=True)
    if isinstance(exc, NotFound):
        return to_error_envelope("not_found", exc.code, exc.message, is_error=True)
    if isinstance(exc, GraphNotAvailable):
        return to_error_envelope("graph_unavailable", exc.code, exc.message, is_error=True)
    # Graph layer errors
    try:
        from chronotrace.graph.errors import GraphConnectionError as _GraphConn  # type: ignore
        from chronotrace.graph.errors import GraphNotAvailable as _GraphNotAvailable  # type: ignore

        if isinstance(exc, (_GraphNotAvailable, _GraphConn)):
            return graph_unavailable_envelope()
    except Exception:
        pass
    # Explicit ServiceUnavailable handling
    try:
        from neo4j.exceptions import ServiceUnavailable, SessionExpired  # type: ignore

        if isinstance(exc, (ServiceUnavailable, SessionExpired)):
            return graph_unavailable_envelope()
    except Exception:
        pass
    name = exc.__class__.__name__.lower()
    msg_lower = str(exc).lower()
    if "serviceunavailable" in name or "sessionexpired" in name or "graphnotavailable" in name:
        return graph_unavailable_envelope()
    if "unavailable" in msg_lower or "connection" in msg_lower or "refused" in msg_lower:
        if (
            "neo4j" in msg_lower
            or "graph" in msg_lower
            or "driver" in msg_lower
            or "database" in msg_lower
        ):
            return graph_unavailable_envelope()
    return internal_error_envelope()
