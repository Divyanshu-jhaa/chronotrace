"""Agent exception hierarchy with secret redaction.

All messages name the env var ``MODEL_API_KEY`` without echoing its value.
``__str__`` and ``__repr__`` redact secret-like substrings defensively.

This module is the single source for agent domain errors and is intentionally
independent from ``chronotrace.config.exceptions`` to avoid circular imports.
Parallel iter-7 workers may import from either hierarchy; error mapping in
``agent.py`` handles both by name and by attribute compatibility.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Patterns that look like leaked secrets (mirrors config.exceptions)
_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{10,}")
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE)
_TOKEN_ASSIGN_RE = re.compile(
    r"(api_key|apikey|password|passwd|secret|token)\s*[:=]\s*[^\s,;\"']+",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE)


def _redact_secrets_in_text(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = _SK_PATTERN.sub(_REDACTED, text)
    text = _SECRET_VALUE_RE.sub(_REDACTED, text)
    text = _BEARER_PATTERN.sub(f"Bearer {_REDACTED}", text)
    try:
        text = _TOKEN_ASSIGN_RE.sub(
            lambda m: f"{m.group(1)}={_REDACTED}",
            text,
        )
    except Exception:
        pass
    return text


class AgentError(Exception):
    """Base for all agent errors. Redacts secrets in string representation."""

    def __str__(self) -> str:  # noqa: D105
        raw = super().__str__() if self.args else ""
        return _redact_secrets_in_text(raw)

    def __repr__(self) -> str:  # noqa: D105
        return f"{self.__class__.__name__}({str(self)!r})"


class AuthenticationError(AgentError):
    """401/403 or missing MODEL_API_KEY — names the env var, never value."""

    def __init__(  # noqa: E501
        self,
        message: str | None = None,
        *args: object,
        secret_name: str = "MODEL_API_KEY",
        **kwargs: object,
    ) -> None:
        # Support both positional and kw construction used by different callers:
        #   AuthenticationError("msg"), AuthenticationError("msg", secret_name="...")
        #   AuthenticationError(message="msg", secret_name="...")
        if message is None and args:
            # first positional arg treated as message
            message = str(args[0])
            args = args[1:]
        if message is None:
            message = "Authentication failed (401/403); check MODEL_API_KEY is set and valid"
        if "MODEL_API_KEY" not in message:
            message = f"{message} (env var MODEL_API_KEY)"
        # If caller passed message via kwargs, ignore (already handled)
        super().__init__(_redact_secrets_in_text(message), *args)
        self.secret_name = secret_name


class RateLimitError(AgentError):
    """429 Too Many Requests — carries Retry-After."""

    def __init__(
        self,
        message: str | None = None,
        *args: object,
        retry_after: int | float | None = None,
        **kwargs: object,
    ) -> None:
        if message is None:
            if retry_after is not None:
                message = f"Rate limit exceeded (429); retry after {retry_after}s"
            else:
                message = "Rate limit exceeded (429); retry after Retry-After header"
        # Allow retry_after passed as positional second arg for legacy callers
        if retry_after is None and args:
            try:
                retry_after = args[0]  # type: ignore[assignment]
                args = args[1:]
            except Exception:
                pass
        super().__init__(_redact_secrets_in_text(message), *args)
        self.retry_after: int | float | None = retry_after


class ProviderError(AgentError):
    """5xx or network failure from model provider."""

    pass


class ModelTimeoutError(AgentError):
    """Request timed out."""

    def __init__(
        self,
        message: str | None = None,
        *args: object,
        timeout_seconds: float | None = None,
        **kwargs: object,
    ) -> None:
        if message is None:
            if timeout_seconds is not None:
                message = (
                    f"Model request timed out after {timeout_seconds}s; "
                    "retry with higher timeout_seconds"
                )
            else:
                message = "Model request timed out"
        if timeout_seconds is None and args:
            try:
                timeout_seconds = float(args[0])  # type: ignore[arg-type]
                args = args[1:]
            except Exception:
                pass
        super().__init__(_redact_secrets_in_text(message), *args)
        self.timeout_seconds = timeout_seconds


class NoFinalResponseError(AgentError):
    """Model returned empty content with no tool calls — never fabricate."""

    pass


class MCPError(AgentError):
    """Transport failure or graph_unavailable from MCP."""

    pass


class ToolValidationError(AgentError):
    """Tool name or arguments failed validation (returned to model as tool error)."""

    pass


class MaxIterationsExceeded(AgentError):
    """Tool loop exceeded max_iterations."""

    def __init__(
        self,
        message: str | None = None,
        *args: object,
        max_iterations: int | None = None,
        **kwargs: object,
    ) -> None:
        if message is None:
            if max_iterations is not None:
                message = f"max_iterations ({max_iterations}) reached"
            else:
                message = "max_iterations exceeded"
        if max_iterations is None and args:
            try:
                max_iterations = int(args[0])  # type: ignore[call-overload]
                args = args[1:]
            except Exception:
                pass
        # Also support max_iterations passed via kwargs without message
        super().__init__(_redact_secrets_in_text(message), *args)
        self.max_iterations: int | None = max_iterations
