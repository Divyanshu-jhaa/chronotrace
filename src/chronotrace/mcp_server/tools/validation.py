"""Shared validation and POSIX path canonicalization for MCP tools.

Implements the strict input validation boundary per Design Decision 3:
- repository_id: required non-empty str 1..256, stable-ID alphabet
- file_path: POSIX-relative, rejects absolute, traversal, backslash, NUL
- limit: integral int 1..max, default when None
- normalize_path: canonical POSIX helper shared with ingestion/static-analysis

All validators raise :class:`ValidationError` from :mod:`chronotrace.mcp_server.tools.errors`
on invalid input. Callers map exceptions to MCP error envelopes.
"""

from __future__ import annotations

import math
import posixpath
import re
from pathlib import Path, PurePosixPath

from .errors import ValidationError

# Stable-ID alphabet: alphanumerics plus - _ : / .
_REPOSITORY_ID_RE = re.compile(r"^[A-Za-z0-9\-_:/\.]+$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:([/\\]|$)")

# Max file_path length (bounded to prevent oversized payloads, fuzz 4096)
_MAX_FILE_PATH_LEN = 4096


def normalize_path(value: str) -> str:
    """Canonical POSIX path normalization shared with ingestion / static-analysis.

    - Converts Windows separators (``\\``) to ``/``.
    - Collapses redundant separators and ``.`` segments via :func:`posixpath.normpath`.
    - Strips leading ``./`` and leading ``/`` where appropriate.
    - Returns ``"."`` for edge case empty after normalization (callers may handle).
    """
    if not isinstance(value, str):
        raise TypeError("file path must be a string")
    if value == "":
        raise ValueError("file path must not be empty")
    # Prefer ingestion implementation for exact parity
    try:
        from chronotrace.ingestion.filtering import normalize_path as _ingest_norm  # type: ignore

        return _ingest_norm(value)
    except Exception:
        pass
    p = value.replace("\\", "/")
    p = posixpath.normpath(p)
    if p == ".":
        return "."
    if p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p.lstrip("/")
    return p


def validate_repository_id(value: object) -> str:
    """Validate and normalize a ``repository_id``."""
    if value is None:
        raise ValidationError(
            "repository_id is required and must be a non-empty string",
            code="VALIDATION_ERROR",
        )
    if not isinstance(value, str):
        raise ValidationError(
            "repository_id must be a string",
            code="VALIDATION_ERROR",
        )
    stripped = value.strip()
    if stripped == "":
        raise ValidationError(
            "repository_id must not be empty or whitespace-only",
            code="VALIDATION_ERROR",
        )
    if len(stripped) < 1 or len(stripped) > 256:
        raise ValidationError(
            "repository_id length must be between 1 and 256 characters",
            code="VALIDATION_ERROR",
        )
    if not _REPOSITORY_ID_RE.fullmatch(stripped):
        raise ValidationError(
            "repository_id contains invalid characters; allowed are "
            "alphanumerics, '-', '_', ':', '/', '.'",
            code="VALIDATION_ERROR",
        )
    return stripped


def validate_file_path(value: object) -> str:
    """Validate and canonicalize a repository-relative POSIX ``file_path``."""
    if value is None:
        raise ValidationError(
            "file_path is required and must be a non-empty string",
            code="VALIDATION_ERROR",
        )
    if not isinstance(value, str):
        raise ValidationError(
            "file_path must be a string",
            code="VALIDATION_ERROR",
        )
    # NUL byte check including literal %00 (encoded null)
    if "\x00" in value:
        raise ValidationError(
            "file_path must not contain NUL byte (\\x00)",
            code="VALIDATION_ERROR",
        )
    if "%00" in value or "%00" in value.lower():
        raise ValidationError(
            "file_path must not contain encoded NUL byte (%00)",
            code="VALIDATION_ERROR",
        )
    if "\\" in value:
        raise ValidationError(
            "file_path must not contain backslash; use POSIX form with '/'",
            code="VALIDATION_ERROR",
        )
    stripped = value.strip()
    if stripped == "":
        raise ValidationError(
            "file_path must not be empty or whitespace-only",
            code="VALIDATION_ERROR",
        )
    if "\x00" in stripped:
        raise ValidationError(
            "file_path must not contain NUL byte (\\x00)",
            code="VALIDATION_ERROR",
        )
    if "%00" in stripped.lower():
        raise ValidationError(
            "file_path must not contain encoded NUL byte (%00)",
            code="VALIDATION_ERROR",
        )
    if "\\" in stripped:
        raise ValidationError(
            "file_path must not contain backslash; use POSIX form with '/'",
            code="VALIDATION_ERROR",
        )
    if stripped.startswith("/"):
        raise ValidationError(
            "file_path must be repository-relative; absolute paths are not allowed",
            code="VALIDATION_ERROR",
        )
    if stripped.startswith("\\\\"):
        raise ValidationError(
            "file_path must be repository-relative; absolute paths are not allowed",
            code="VALIDATION_ERROR",
        )
    if _WINDOWS_DRIVE_RE.match(stripped):
        raise ValidationError(
            "file_path must be repository-relative; Windows absolute paths are not allowed",
            code="VALIDATION_ERROR",
        )
    if len(stripped) > _MAX_FILE_PATH_LEN:
        raise ValidationError(
            f"file_path exceeds maximum length of {_MAX_FILE_PATH_LEN} characters",
            code="VALIDATION_ERROR",
        )
    raw_segments = stripped.split("/")
    for seg in raw_segments:
        if seg == "..":
            raise ValidationError(
                "file_path must not contain parent traversal '..' segments",
                code="VALIDATION_ERROR",
            )
        if "\x00" in seg:
            raise ValidationError(
                "file_path must not contain NUL byte (\\x00)",
                code="VALIDATION_ERROR",
            )
    try:
        posix = Path(stripped).as_posix()
    except Exception as exc:
        raise ValidationError(f"file_path is invalid: {exc}", code="VALIDATION_ERROR") from exc
    try:
        if PurePosixPath(posix).is_absolute():
            raise ValidationError(
                "file_path must be repository-relative; absolute paths are not allowed",
                code="VALIDATION_ERROR",
            )
    except ValidationError:
        raise
    except Exception:
        pass
    if posix.startswith("/"):
        raise ValidationError(
            "file_path must be repository-relative; absolute paths are not allowed",
            code="VALIDATION_ERROR",
        )
    if _WINDOWS_DRIVE_RE.match(posix):
        raise ValidationError(
            "file_path must be repository-relative; Windows absolute paths are not allowed",
            code="VALIDATION_ERROR",
        )
    if "\x00" in posix:
        raise ValidationError(
            "file_path must not contain NUL byte (\\x00)",
            code="VALIDATION_ERROR",
        )
    if "%00" in posix.lower():
        raise ValidationError(
            "file_path must not contain encoded NUL byte (%00)",
            code="VALIDATION_ERROR",
        )
    if "\\" in posix:
        raise ValidationError(
            "file_path must not contain backslash; use POSIX form with '/'",
            code="VALIDATION_ERROR",
        )
    for seg in posix.split("/"):
        if seg == "..":
            raise ValidationError(
                "file_path must not contain parent traversal '..' segments",
                code="VALIDATION_ERROR",
            )
    normalized = posixpath.normpath(posix)
    if normalized == "." or normalized == "":
        raise ValidationError(
            "file_path must not be '.' or empty; provide a repository-relative file path",
            code="VALIDATION_ERROR",
        )
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith("/"):
        raise ValidationError(
            "file_path must be repository-relative; absolute paths are not allowed",
            code="VALIDATION_ERROR",
        )
    if "\x00" in normalized or "%00" in normalized.lower():
        raise ValidationError(
            "file_path must not contain NUL byte (\\x00)",
            code="VALIDATION_ERROR",
        )
    if "\\" in normalized:
        raise ValidationError(
            "file_path must not contain backslash; use POSIX form with '/'",
            code="VALIDATION_ERROR",
        )
    for seg in normalized.split("/"):
        if seg == "..":
            raise ValidationError(
                "file_path must not contain parent traversal '..' segments",
                code="VALIDATION_ERROR",
            )
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise ValidationError(
            "file_path must not contain parent traversal '..' segments",
            code="VALIDATION_ERROR",
        )
    if len(normalized) > _MAX_FILE_PATH_LEN:
        raise ValidationError(
            f"file_path exceeds maximum length of {_MAX_FILE_PATH_LEN} characters",
            code="VALIDATION_ERROR",
        )
    return normalized


def validate_limit(
    value: object,
    default: int = 20,
    max_limit: int = 100,
) -> int:
    """Validate an optional bounded ``limit`` for ``get_file_history``."""
    if value is None:
        if not isinstance(default, int) or isinstance(default, bool):
            raise ValidationError("default limit must be an int", code="VALIDATION_ERROR")
        if not isinstance(max_limit, int) or isinstance(max_limit, bool):
            raise ValidationError("max_limit must be an int", code="VALIDATION_ERROR")
        return default
    if isinstance(value, bool):
        raise ValidationError(
            "limit must be an integer between 1 and 100",
            code="VALIDATION_ERROR",
        )
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValidationError(
                "limit must be an integer between 1 and 100; NaN/Infinity not allowed",
                code="VALIDATION_ERROR",
            )
        raise ValidationError(
            "limit must be an integer; floats are not allowed",
            code="VALIDATION_ERROR",
        )
    if isinstance(value, str):
        # String "Infinity", "NaN", "20" all rejected
        lowered = value.strip().lower()
        if lowered in ("inf", "infinity", "nan", "+inf", "-inf"):
            raise ValidationError(
                "limit must be an integer between 1 and 100; NaN/Infinity not allowed",
                code="VALIDATION_ERROR",
            )
        raise ValidationError(
            "limit must be an integer between 1 and 100; strings are not allowed",
            code="VALIDATION_ERROR",
        )
    if not isinstance(value, int):
        raise ValidationError(
            "limit must be an integer between 1 and 100",
            code="VALIDATION_ERROR",
        )
    if value < 1:
        raise ValidationError(
            "limit must be between 1 and 100; 0 and negative values are not allowed",
            code="VALIDATION_ERROR",
        )
    if value > max_limit:
        raise ValidationError(
            f"limit must be between 1 and {max_limit}; {value} exceeds maximum",
            code="VALIDATION_ERROR",
        )
    return value
