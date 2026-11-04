"""Report request/response schemas — POSIX file_path validation and analytics contract."""

from __future__ import annotations

import re
import urllib.parse
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict

from chronotrace.mcp_server.tools.errors import ValidationError as _McpValidationError
from chronotrace.mcp_server.tools.validation import validate_file_path as _mcp_validate_file_path
from chronotrace.mcp_server.tools.validation import validate_repository_id as _mcp_validate_repo_id

# ---------------------------------------------------------------------------
# Helpers that adapt MCP validators to Pydantic AfterValidator (ValueError)
# ---------------------------------------------------------------------------

_REPOSITORY_ID_RE = re.compile(r"^[A-Za-z0-9\-_:/\.]+$")


def _validate_repository_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("repository_id must be a string")
    if "\x00" in value:
        raise ValueError("repository_id must not contain NUL byte")
    stripped = value.strip()
    if stripped == "":
        raise ValueError("repository_id is required and must be a non-empty string")
    if len(stripped) < 1 or len(stripped) > 256:
        raise ValueError("repository_id length must be between 1 and 256 characters")
    if not _REPOSITORY_ID_RE.fullmatch(stripped):
        raise ValueError(
            "repository_id contains invalid characters; allowed are alphanumerics, '-', '_', ':', '/', '.'"
        )
    # Delegate to MCP validator for parity (will raise ValidationError if invalid)
    try:
        return _mcp_validate_repo_id(value)
    except _McpValidationError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        raise ValueError(str(e)) from e


def _validate_file_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("file_path must be a string")
    # Early NUL / backslash checks (mirror MCP validation)
    if "\x00" in value:
        raise ValueError("file_path must not contain NUL byte (\\x00)")
    if "%00" in value.lower():
        raise ValueError("file_path must not contain encoded NUL byte (%00)")
    if "\\" in value:
        raise ValueError("file_path must not contain backslash; use POSIX form with '/'")
    # Check for URL-encoded traversal before delegating
    # If value contains %2e / %2f / %5c, decode and see if it would be traversal/absolute
    lowered = value.lower()
    if "%2e" in lowered or "%2f" in lowered or "%5c" in lowered:
        try:
            decoded = urllib.parse.unquote(value)
        except Exception:
            decoded = value
        # If decoded contains traversal or absolute or backslash, reject
        if decoded != value:
            if "\x00" in decoded or "%00" in decoded.lower():
                raise ValueError("file_path must not contain NUL byte (\\x00)")
            if "\\" in decoded:
                raise ValueError("file_path must not contain backslash; use POSIX form with '/'")
            if decoded.strip().startswith("/"):
                raise ValueError(
                    "file_path must be repository-relative; absolute paths are not allowed"
                )
            if ".." in decoded.split("/"):
                raise ValueError("file_path must not contain parent traversal '..' segments")
            # Encoded separators themselves are rejected even if decoded is benign,
            # to prevent bypass via encoding.
            raise ValueError("file_path must not contain encoded traversal sequences (%2e/%2f/%5c)")
    # Delegate to canonical MCP validator (handles absolute, traversal, length, etc.)
    try:
        return _mcp_validate_file_path(value)
    except _McpValidationError as e:
        raise ValueError(str(e)) from e
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(str(e)) from e


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ReportRequest(BaseModel):
    """POST /reports request — repository_id + file_path (POSIX, traversal-safe)."""

    model_config = ConfigDict(extra="forbid")

    repository_id: Annotated[str, AfterValidator(_validate_repository_id)]
    file_path: Annotated[str, AfterValidator(_validate_file_path)]


class ReportResponse(BaseModel):
    """Report response — wraps analytics common contract."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "insufficient_history", "not_found", "error"]
    repository_id: str
    target: str
    result: Any | None = None
    evidence: Any | None = None
    limitations: Any | None = None

    # Allow optional request_id for tracing? Keep strict, but allow extra via model_extra if needed
    # For now keep forbid; clients that send extra will get 422, which is desired.


__all__ = [
    "ReportRequest",
    "ReportResponse",
    "_validate_file_path",
    "_validate_repository_id",
]
