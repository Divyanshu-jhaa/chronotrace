"""MCP tools — shared validation and error mapping.

Consumes validated ChronTraceConfig only via explicit injection, never re-parses YAML.
"""

from __future__ import annotations

from .errors import GraphNotAvailable, NotFound, ValidationError, map_error, to_error_envelope
from .validation import normalize_path, validate_file_path, validate_limit, validate_repository_id

__all__ = [
    "CANONICAL_SCHEMAS",
    "GraphNotAvailable",
    "NotFound",
    "SCHEMAS",
    "ValidationError",
    "map_error",
    "normalize_path",
    "to_error_envelope",
    "to_provider_tools",
    "validate_file_path",
    "validate_limit",
    "validate_repository_id",
]

try:
    from .schemas import CANONICAL_SCHEMAS, SCHEMAS, to_provider_tools

    __all__ = [
        "CANONICAL_SCHEMAS",
        "GraphNotAvailable",
        "NotFound",
        "SCHEMAS",
        "ValidationError",
        "map_error",
        "normalize_path",
        "to_error_envelope",
        "to_provider_tools",
        "validate_file_path",
        "validate_limit",
        "validate_repository_id",
    ]
except Exception:
    pass
