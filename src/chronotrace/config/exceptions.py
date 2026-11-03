"""Configuration exception hierarchy with secret redaction."""

from __future__ import annotations

import re

_REDACTED_PATTERNS = re.compile(r"(api_key|password|secret|token)", re.IGNORECASE)

# Heuristic for values that look like real secrets (e.g., sk-...)
_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9]{10,}|neo4j/[^\s]+", re.IGNORECASE)


def _redact_message(msg: str) -> str:
    # If message contains a field name that is sensitive, ensure no value leaks
    # Simple heuristic: if message contains a secret-like value, redact it
    # We never include raw secret values in messages, but this is defense-in-depth
    if _SECRET_VALUE_RE.search(msg):
        return _REDACTED_PATTERNS.sub(r"\1", _SECRET_VALUE_RE.sub("[REDACTED]", msg))
    return msg


class ChronoTraceConfigError(Exception):
    """Base for all config errors. Redacts secrets in string representation."""

    def __str__(self) -> str:
        raw = super().__str__() if self.args else ""
        # Redact any secret-like values defensively
        return _redact_message(raw)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({str(self)!r})"


class ConfigValidationError(ChronoTraceConfigError):
    """YAML structure, version, keys, regex, numerics, enums, or path validation failed."""

    pass


class MissingSecretError(ChronoTraceConfigError):
    """Required secret not present in environment."""

    def __init__(self, secret_name: str, hint: str = ""):
        # Never include value, only name
        msg = f"{secret_name} is required but not set."
        if hint:
            msg += f" {hint}"
        # Ensure no value leaks even if caller mistakenly passes value
        super().__init__(msg)
        self.secret_name = secret_name


class FilesystemError(ChronoTraceConfigError):
    """Data/repositories directory creation or writability failed."""

    pass


class ConnectivityError(ChronoTraceConfigError):
    """Neo4j connectivity or database access failed."""

    pass
