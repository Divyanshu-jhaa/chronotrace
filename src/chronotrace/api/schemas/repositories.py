"""Repository request/response schemas — typed validation for iter-8."""

from __future__ import annotations

import re
import urllib.parse
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.functional_validators import AfterValidator


def validate_git_url(value: str) -> str:
    """Validate Git URL per spec: http/https/ssh, git@, file, length 1-2048, no NUL, no shell interpolation."""
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    # Check raw value for NUL and shell metachars before stripping — trailing \r\n must be rejected
    if "\x00" in value:
        raise ValueError("url must not contain NUL byte (\\x00)")
    if "\n" in value or "\r" in value:
        raise ValueError("url must not contain newline")
    stripped = value.strip()
    if not stripped:
        raise ValueError("url must not be empty")
    if len(stripped) < 1 or len(stripped) > 2048:
        raise ValueError("url length must be between 1 and 2048 characters")
    if "\x00" in stripped:
        raise ValueError("url must not contain NUL byte (\\x00)")
    # Reject shell interpolation / metacharacters
    if any(tok in stripped for tok in ["$(", "`", "${"]):
        raise ValueError("url must not contain shell interpolation")
    # More granular: if url contains $, `, ;, |, &, \n, \r, reject
    # But to avoid rejecting valid urls with query strings containing &, we only reject if those appear outside valid url encoding?
    # Simplistic: git urls rarely contain &, ;, |, $, `, so reject them
    # However https://github.com/org/repo?arg=1&arg2=2 could contain &, but not typical for repo clone url
    # For MVP, reject bare $, `, ;, |, \n, \r ; allow & only if part of query? We'll treat & as suspicious for clone urls
    # Spec says reject shell-interpolation strings; example includes shell metachars, so we reject those
    suspicious = False
    if "$" in stripped or "`" in stripped:
        suspicious = True
    if ";" in stripped or "|" in stripped:
        suspicious = True
    # Check for shell sequences like "&&", "||", ">>", etc
    if "&&" in stripped or "||" in stripped or "`" in stripped:
        suspicious = True
    if "\n" in stripped or "\r" in stripped:
        suspicious = True
    # Also reject backtick
    if "`" in stripped:
        suspicious = True
    if suspicious:
        raise ValueError("url must not contain shell metacharacters")
    # Validate shape
    if stripped.startswith("git@"):
        if not re.match(r"^git@[^:\s]+:[^\s]+$", stripped):
            raise ValueError("invalid git@ URL shape; expected git@host:owner/repo")
        # Additional slash check: path part should not contain spaces
        if " " in stripped:
            raise ValueError("url must not contain spaces")
        return stripped
    # Standard URL parse
    try:
        parsed = urllib.parse.urlparse(stripped)
    except Exception as e:
        raise ValueError(f"invalid URL: {e}") from e
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("https", "http", "ssh", "file"):
        raise ValueError(
            f"unsupported URL scheme {scheme!r}; expected https, http, ssh, git@, or file"
        )
    if scheme != "file":
        if not parsed.hostname and not parsed.netloc:
            raise ValueError("URL missing host")
        if not parsed.hostname:
            raise ValueError("URL missing host")
    if " " in stripped:
        raise ValueError("url must not contain spaces")
    # Reject urls with embedded credentials that look like shell? Already handled
    return stripped


class RepositoryRegisterRequest(BaseModel):
    """POST /repositories request — validates Git URL and optional branch."""

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, AfterValidator(validate_git_url)]
    branch: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("branch")
    @classmethod
    def _validate_branch(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("branch must not be empty or whitespace-only")
        if "\x00" in v:
            raise ValueError("branch must not contain NUL byte")
        if len(stripped) > 256:
            raise ValueError("branch exceeds maximum length of 256 characters")
        return stripped


class RepositoryResponse(BaseModel):
    """POST /repositories response — 201 with identifiers and status."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    url: str | None = None
    branch: str | None = None


class RepositoryListItem(BaseModel):
    """Single repository in listing."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str
    url: str
    ingestion_status: str | None = None
    status: str | None = None
    last_ingested_at: str | None = None
    head: str | None = None
    job_id: str | None = None
    updated_at: str | None = None


class RepositoryListResponse(BaseModel):
    """GET /repositories response — bounded pagination."""

    model_config = ConfigDict(extra="forbid")

    repositories: list[RepositoryListItem]
    total: int | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class RepositoryStatusResponse(BaseModel):
    """GET /repositories/{id} or GET /jobs/{id} — job status detail."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    stage: str
    progress: float | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None
    repository_url: str | None = None
    url: str | None = None
    branch: str | None = None
    last_ingested_at: str | None = None
    head: str | None = None


# Back-compat aliases expected by other modules / tests
RepositoryCreateResponse = RepositoryResponse
RepositoryDetailResponse = RepositoryStatusResponse
JobResponse = RepositoryStatusResponse


__all__ = [
    "validate_git_url",
    "RepositoryRegisterRequest",
    "RepositoryResponse",
    "RepositoryCreateResponse",
    "RepositoryListItem",
    "RepositoryListResponse",
    "RepositoryStatusResponse",
    "RepositoryDetailResponse",
    "JobResponse",
]
