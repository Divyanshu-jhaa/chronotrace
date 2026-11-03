"""Ingestion domain exceptions with stage tracking and secret redaction."""

from __future__ import annotations

import re


def _redact_secrets(msg: str) -> str:
    """Redact credentials from messages."""
    # Redact user:pass@ patterns
    msg = re.sub(r"://[^@\s]+:[^@\s]+@", "://[REDACTED]@", msg)
    msg = re.sub(r"://[^@\s]+@", "://[REDACTED]@", msg)
    # Redact password/secret/token values if leaked
    lower = msg.lower()
    for token in ("password", "passwd", "secret", "token", "api_key"):
        if token in lower:
            # If message contains potential secret value after token, redact
            msg = re.sub(rf"{token}[\s=:]+[^\s]+", f"{token}=[REDACTED]", msg, flags=re.IGNORECASE)
    return msg


class IngestionError(Exception):
    """Base ingestion error — never leaks credentials."""

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        sanitized = _redact_secrets(message)
        super().__init__(sanitized)
        self.stage = stage

    def __str__(self) -> str:
        return _redact_secrets(super().__str__())


class AcquisitionError(IngestionError):
    """Clone/fetch acquisition failure."""


class ExtractionError(IngestionError):
    """PyDriller extraction failure."""


class FilteringError(IngestionError):
    """Filtering failure."""


class LabelingError(IngestionError):
    """Labeling failure."""


class GraphWriteError(IngestionError):
    """Graph persistence failure."""


class RewrittenHistoryError(IngestionError):
    """Detected force-push / rewritten history requiring full re-ingestion."""

    def __init__(self, message: str, *, needs_full_reingestion: bool = True) -> None:
        super().__init__(message, stage="EXTRACTING")
        self.needs_full_reingestion = needs_full_reingestion


class ValidationError(IngestionError):
    """Repository URL or config validation failure."""
