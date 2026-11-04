"""Shared error envelope and redaction for API — strict schemas and secret-safe serialization.

Implements tasks 2.1: ErrorDetail, ErrorEnvelope, code mapping, redaction helper.
All error responses use envelope ``{error: {code, message, details?}, request_id?}``
with consistent codes and secret redaction.

Secrets must never appear verbatim in serialized errors.  The helper strips values
whose key contains api_key/password/secret/token/auth/uri and strips connection
strings.  It also ensures SQL-like injection payloads (``' OR 1=1``) are not
echoed verbatim.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# ---------------------------------------------------------------------------
# Code mapping
# ---------------------------------------------------------------------------

ErrorCode = Literal[
    "validation_error",
    "not_found",
    "graph_unavailable",
    "model_unavailable",
    "mcp_unavailable",
    "not_ready",
    "internal_error",
    "internal",
]

VALIDATION_ERROR = "validation_error"
NOT_FOUND = "not_found"
GRAPH_UNAVAILABLE = "graph_unavailable"
MODEL_UNAVAILABLE = "model_unavailable"
MCP_UNAVAILABLE = "mcp_unavailable"
NOT_READY = "not_ready"
INTERNAL_ERROR = "internal_error"

_CODE_FOR_STATUS: dict[int, str] = {
    400: VALIDATION_ERROR,
    413: VALIDATION_ERROR,
    422: VALIDATION_ERROR,
    404: NOT_FOUND,
    503: GRAPH_UNAVAILABLE,
    504: MODEL_UNAVAILABLE,
    500: INTERNAL_ERROR,
}


def code_for_status(status_code: int) -> str:
    """Map HTTP status to envelope code."""
    return _CODE_FOR_STATUS.get(int(status_code), INTERNAL_ERROR)


# ---------------------------------------------------------------------------
# Redaction / sanitization
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "api-key",
    "password",
    "passwd",
    "secret",
    "token",
    "auth",
    "credential",
    "private",
)

# Pattern for URI with credentials like bolt://user:pass@host or https://token@host
_SECRET_URI_RE = re.compile(r"://[^@\s]+:[^@\s]+@")
_SECRET_URI_USER_RE = re.compile(r"://[^@\s]+@")
_SENSITIVE_KV_RE = re.compile(
    r"(\b(?:" + "|".join(re.escape(k) for k in _SENSITIVE_KEYS) + r")\b)\s*[:=]\s*[^\s,;\"']+",
    flags=re.IGNORECASE,
)
_BOLT_RE = re.compile(r"bolt://[^\s]+", flags=re.IGNORECASE)
_NEO4J_RE = re.compile(r"neo4j://[^\s]+", flags=re.IGNORECASE)
_SQL_INJECTION_RE = re.compile(r"' OR\s+1\s*=\s*1[^'\n]*", flags=re.IGNORECASE)
_SQL_OR_RE = re.compile(r"' OR\s+'[^']+'\s*=\s*'[^']+'", flags=re.IGNORECASE)

# Additional hard-coded secret tokens that must be redacted
_SENSITIVE_TOKENS = ("NEO4J_PASSWORD", "MODEL_API_KEY", "NEO4J_USERNAME", "NEO4J_URI", "API_KEY")


def _sanitize_str(value: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    # Strip SQL injection verbatim
    if "' OR 1=1" in value or "' OR" in value.upper():
        # Replace common injection payloads
        value = _SQL_INJECTION_RE.sub("[REDACTED]", value)
        value = _SQL_OR_RE.sub("[REDACTED]", value)
        # Fallback: if still contains the literal, replace
        if "' OR 1=1" in value:
            value = value.replace("' OR 1=1", "[REDACTED]")
        if "' OR" in value.upper():
            # Generic redaction for any ' OR pattern
            try:
                value = re.sub(r"' OR[^;]*", "[REDACTED]", value, flags=re.IGNORECASE)
            except Exception:
                pass
    # Secret tokens with optional value (e.g., NEO4J_PASSWORD=secret123)
    for tok in _SENSITIVE_TOKENS:
        if tok.lower() in value.lower():
            try:
                # Match token with optional separator and value, redact the whole
                value = re.sub(
                    rf"{re.escape(tok)}\s*[:=]\s*[^\s,;\"']+",
                    f"{tok}=[REDACTED]",
                    value,
                    flags=re.IGNORECASE,
                )
                # Also replace bare token without value
                value = re.sub(re.escape(tok), "[REDACTED]", value, flags=re.IGNORECASE)
            except Exception:
                pass
    # URI credentials
    try:
        value = _SECRET_URI_RE.sub("://[REDACTED]@", value)
        value = _SECRET_URI_USER_RE.sub("://[REDACTED]@", value)
        value = _BOLT_RE.sub("bolt://[REDACTED]", value)
        value = _NEO4J_RE.sub("neo4j://[REDACTED]", value)
    except Exception:
        pass
    # Key=value secrets
    try:
        value = _SENSITIVE_KV_RE.sub(r"\1=[REDACTED]", value)
    except Exception:
        pass
    # Also redact generic password/secret patterns with =
    try:
        value = re.sub(
            r"(password|passwd|secret|token|api_key|auth)\s*=\s*[^\s,;]+",
            r"\1=[REDACTED]",
            value,
            flags=re.IGNORECASE,
        )
    except Exception:
        pass
    # Ensure no raw secret substring remains if it was like "secret123" after key
    # Already handled via KV; keep.
    return value


def redact_secrets(value: Any) -> Any:
    """Recursively redact secrets and injection payloads in any value."""
    if isinstance(value, str):
        return _sanitize_str(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            # If key itself is sensitive, redact value entirely
            if isinstance(k, str) and any(s in k.lower() for s in _SENSITIVE_KEYS):
                # For dict/list values under sensitive key, redact recursively but ensure no secret leaks
                if isinstance(v, (dict, list, tuple, set)):
                    # redact nested but also mark as redacted
                    out[k] = "[REDACTED]"
                else:
                    out[k] = "[REDACTED]"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(value, (list, tuple, set)):
        t = type(value)
        try:
            return t(redact_secrets(v) for v in value)  # type: ignore[call-arg]
        except Exception:
            return [redact_secrets(v) for v in value]  # type: ignore[return-value]
    return value


def sanitize_message(message: str) -> str:
    """Public helper: sanitize a single message string."""
    return _sanitize_str(message)


# Back-compat aliases
_redact_secrets = _sanitize_str
_sanitize_message = _sanitize_str


def _sanitize_any(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return _sanitize_str(value)
    if isinstance(value, dict):
        return {k: _sanitize_any(v) for k, v in value.items()}  # type: ignore[no-untyped-call]
    if isinstance(value, list):
        return [_sanitize_any(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_any(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    """Detail for the error envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: Any | None = None

    @field_validator("code")
    @classmethod
    def _validate_code(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("code must be a non-empty string")
        # Normalize to snake_case lower
        return v.strip()

    @field_validator("message")
    @classmethod
    def _validate_message(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("message must be a string")
        # Sanitize before storing
        return _sanitize_str(v)

    @model_validator(mode="before")
    @classmethod
    def _sanitize_before(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Sanitize message and details if present
            if "message" in data and isinstance(data["message"], str):
                data = dict(data)
                data["message"] = _sanitize_str(data["message"])
            if "details" in data and data["details"] is not None:
                data = dict(data)
                data["details"] = _sanitize_any(data["details"])
        return data

    @model_validator(mode="after")
    def _sanitize_after(self) -> ErrorDetail:
        # Ensure stored values are sanitized (defense in depth)
        object.__setattr__(self, "message", _sanitize_str(self.message))
        if self.details is not None:
            object.__setattr__(self, "details", _sanitize_any(self.details))
        return self


class ErrorEnvelope(BaseModel):
    """Top-level error envelope."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
    request_id: str | None = None

    @field_validator("request_id")
    @classmethod
    def _validate_request_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("request_id must be a string")
        return _sanitize_str(v)

    @model_validator(mode="before")
    @classmethod
    def _sanitize_before(cls, data: Any) -> Any:
        if isinstance(data, dict) and "error" in data:
            # Ensure nested error is sanitized even if passed as dict
            err = data["error"]
            if isinstance(err, dict):
                data = dict(data)
                # Sanitize message/details inside error dict
                if "message" in err and isinstance(err["message"], str):
                    err = dict(err)
                    err["message"] = _sanitize_str(err["message"])
                    data["error"] = err
                if "details" in err and err["details"] is not None:
                    err = dict(data["error"])
                    err["details"] = _sanitize_any(err["details"])
                    data["error"] = err
        return data


# ---------------------------------------------------------------------------
# Convenience envelope builders
# ---------------------------------------------------------------------------


def validation_error_envelope(
    message: str, details: Any | None = None, request_id: str | None = None
) -> dict[str, Any]:
    env = ErrorEnvelope(
        error=ErrorDetail(code=VALIDATION_ERROR, message=message, details=details),
        request_id=request_id,
    )
    return env.model_dump(mode="json", exclude_none=True)


def not_found_envelope(
    message: str, details: Any | None = None, request_id: str | None = None
) -> dict[str, Any]:
    env = ErrorEnvelope(
        error=ErrorDetail(code=NOT_FOUND, message=message, details=details), request_id=request_id
    )
    return env.model_dump(mode="json", exclude_none=True)


def graph_unavailable_envelope(
    message: str = "Graph is temporarily unavailable; retry", request_id: str | None = None
) -> dict[str, Any]:
    env = ErrorEnvelope(
        error=ErrorDetail(code=GRAPH_UNAVAILABLE, message=message), request_id=request_id
    )
    return env.model_dump(mode="json", exclude_none=True)


def model_unavailable_envelope(
    message: str = "Model is temporarily unavailable; retry", request_id: str | None = None
) -> dict[str, Any]:
    env = ErrorEnvelope(
        error=ErrorDetail(code=MODEL_UNAVAILABLE, message=message), request_id=request_id
    )
    return env.model_dump(mode="json", exclude_none=True)


def internal_error_envelope(
    message: str = "Internal error", request_id: str | None = None
) -> dict[str, Any]:
    # Always sanitize and do not echo raw internal details
    safe = _sanitize_str(message) if message != "Internal error" else "Internal error"
    # If caller passes secret, sanitize will redact; we also force generic if not safe?
    env = ErrorEnvelope(error=ErrorDetail(code=INTERNAL_ERROR, message=safe), request_id=request_id)
    return env.model_dump(mode="json", exclude_none=True)


__all__ = [
    "ErrorDetail",
    "ErrorEnvelope",
    "VALIDATION_ERROR",
    "NOT_FOUND",
    "GRAPH_UNAVAILABLE",
    "MODEL_UNAVAILABLE",
    "MCP_UNAVAILABLE",
    "NOT_READY",
    "INTERNAL_ERROR",
    "code_for_status",
    "_sanitize_str",
    "_sanitize_any",
    "redact_secrets",
    "sanitize_message",
    "validation_error_envelope",
    "not_found_envelope",
    "graph_unavailable_envelope",
    "model_unavailable_envelope",
    "internal_error_envelope",
]
