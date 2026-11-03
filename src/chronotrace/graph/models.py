"""Domain models for graph nodes with stable repository-scoped identifiers."""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def canonical_repository_id(remote_url: str | None, explicit_id: str | None = None) -> str:
    """Derive a stable repository ID.

    If explicit_id is provided, it is returned as-is (trimmed).
    Otherwise canonicalize remote_url: lowercase, strip whitespace, remove
    trailing slashes and trailing .git suffix.  If remote_url is empty,
    raise ValueError — callers must supply one of the two.
    """
    if explicit_id is not None and explicit_id.strip() != "":
        return explicit_id.strip()
    if remote_url is None or remote_url.strip() == "":
        raise ValueError("repository id requires remote_url or explicit_id")
    url = remote_url.strip().lower()
    # Remove trailing slashes
    url = url.rstrip("/")
    # Remove trailing .git (case-insensitive, already lowercased)
    if url.endswith(".git"):
        url = url[:-4]
        url = url.rstrip("/")
    if not url:
        raise ValueError("canonical repository id is empty after normalization")
    return url


def normalize_path(path: str) -> str:
    """Normalize a file path to POSIX form.

    - Converts Windows separators (``\\``) to ``/``.
    - Collapses ``.`` and ``..``-like redundant separators via posixpath.normpath.
    - Strips leading ``./`` and ``/`` where appropriate, preserving relative semantics
      but ensuring consistent stable IDs.
    - Returns ``"."`` as ``"."`` (edge case) — callers should avoid empty.
    """
    if path == "":
        raise ValueError("file path must not be empty")
    # Convert Windows separators
    # Use PureWindowsPath to handle drive letters and backslashes uniformly,
    # then convert to posix.
    # Simpler: replace backslash with slash
    p = path.replace("\\", "/")
    # posixpath.normpath collapses //, ./, etc.
    p = posixpath.normpath(p)
    # normpath turns "" into "."; preserve single dot handling
    # Remove leading ./ if present after normpath (e.g., "./a/b" -> "a/b")
    if p.startswith("./"):
        p = p[2:]
    return p


def derive_commit_id(repository_id: str, commit_hash: str) -> str:
    """Stable commit ID: ``{repository_id}:{hash}``."""
    if not repository_id:
        raise ValueError("repository_id must not be empty")
    if not commit_hash:
        raise ValueError("commit_hash must not be empty")
    h = commit_hash.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,40}", h):
        # Allow any non-empty hash but warn; we still derive ID
        pass
    return f"{repository_id}:{h}"


def derive_file_id(repository_id: str, file_path: str) -> str:
    """Stable file ID: ``{repository_id}:{posix_path}``."""
    if not repository_id:
        raise ValueError("repository_id must not be empty")
    norm = normalize_path(file_path)
    return f"{repository_id}:{norm}"


def derive_author_id(repository_id: str, email: str | None, name: str | None) -> str:
    """Stable author ID scoped to repository.

    Uses ``email`` when available (lowercased, trimmed), otherwise ``name``.
    The key is hashed to keep IDs bounded and safe for Cypher.
    """
    if not repository_id:
        raise ValueError("repository_id must not be empty")
    if email and email.strip():
        key = email.strip().lower()
    elif name and name.strip():
        key = name.strip().lower()
    else:
        raise ValueError("author requires email or name")
    # Hash to avoid special characters in ID
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{repository_id}:author:{digest}"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Repository:
    """Repository node."""

    id: str
    repository_id: str
    name: str
    remote_url: str | None = None
    default_branch: str | None = None
    current_head: str | None = None
    last_ingested_at: str | None = None

    @staticmethod
    def create(
        *,
        name: str,
        remote_url: str | None = None,
        repository_id: str | None = None,
        default_branch: str | None = None,
        current_head: str | None = None,
        last_ingested_at: str | None = None,
    ) -> Repository:
        rid = canonical_repository_id(remote_url, repository_id)
        return Repository(
            id=rid,
            repository_id=rid,
            name=name,
            remote_url=remote_url,
            default_branch=default_branch,
            current_head=current_head,
            last_ingested_at=last_ingested_at,
        )

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "name": self.name,
            "remote_url": self.remote_url,
            "default_branch": self.default_branch,
            "current_head": self.current_head,
            "last_ingested_at": self.last_ingested_at,
        }

    @staticmethod
    def from_record(record: dict[str, Any]) -> Repository:
        return Repository(
            id=record["id"],
            repository_id=record.get("repository_id", record["id"]),
            name=record["name"],
            remote_url=record.get("remote_url"),
            default_branch=record.get("default_branch"),
            current_head=record.get("current_head"),
            last_ingested_at=record.get("last_ingested_at"),
        )


@dataclass(frozen=True, slots=True)
class Author:
    """Author node — scoped to repository."""

    id: str
    repository_id: str
    name: str | None = None
    email: str | None = None

    @staticmethod
    def create(
        *,
        repository_id: str,
        name: str | None = None,
        email: str | None = None,
        author_id: str | None = None,
    ) -> Author:
        if author_id is not None and author_id.strip():
            aid = author_id.strip()
        else:
            aid = derive_author_id(repository_id, email, name)
        return Author(id=aid, repository_id=repository_id, name=name, email=email)

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "name": self.name,
            "email": self.email,
        }

    @staticmethod
    def from_record(record: dict[str, Any]) -> Author:
        return Author(
            id=record["id"],
            repository_id=record["repository_id"],
            name=record.get("name"),
            email=record.get("email"),
        )


@dataclass(frozen=True, slots=True)
class Commit:
    """Commit node."""

    id: str
    repository_id: str
    hash: str
    date: str | None = None
    message: str | None = None
    is_fix: bool = False
    is_revert: bool = False
    changed_file_count: int | None = None
    author_id: str | None = None

    @staticmethod
    def create(
        *,
        repository_id: str,
        hash: str,
        date: str | datetime | None = None,
        message: str | None = None,
        is_fix: bool = False,
        is_revert: bool = False,
        changed_file_count: int | None = None,
        author_id: str | None = None,
        commit_id: str | None = None,
    ) -> Commit:
        # Normalize date to ISO string if datetime
        date_str: str | None = None
        if isinstance(date, datetime):
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            date_str = date.isoformat()
        elif date is not None:
            date_str = str(date)
        full_hash = hash.strip().lower()
        cid = (
            commit_id.strip()
            if commit_id and commit_id.strip()
            else derive_commit_id(repository_id, full_hash)
        )
        return Commit(
            id=cid,
            repository_id=repository_id,
            hash=full_hash,
            date=date_str,
            message=message,
            is_fix=bool(is_fix),
            is_revert=bool(is_revert),
            changed_file_count=changed_file_count,
            author_id=author_id,
        )

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "hash": self.hash,
            "date": self.date,
            "message": self.message,
            "is_fix": self.is_fix,
            "is_revert": self.is_revert,
            "changed_file_count": self.changed_file_count,
            "author_id": self.author_id,
        }

    @staticmethod
    def from_record(record: dict[str, Any]) -> Commit:
        return Commit(
            id=record["id"],
            repository_id=record["repository_id"],
            hash=record["hash"],
            date=record.get("date"),
            message=record.get("message"),
            is_fix=bool(record.get("is_fix", False)),
            is_revert=bool(record.get("is_revert", False)),
            changed_file_count=record.get("changed_file_count"),
            author_id=record.get("author_id"),
        )


@dataclass(frozen=True, slots=True)
class File:
    """File node — tracks current path, history, and lifecycle flags."""

    id: str
    repository_id: str
    path: str
    current_path: str
    past_paths: list[str] = field(default_factory=list)
    alive: bool = True
    is_test: bool = False
    language: str | None = None

    @staticmethod
    def create(
        *,
        repository_id: str,
        path: str,
        past_paths: list[str] | None = None,
        alive: bool = True,
        is_test: bool = False,
        language: str | None = None,
        file_id: str | None = None,
    ) -> File:
        norm = normalize_path(path)
        fid = (
            file_id.strip()
            if file_id and file_id.strip()
            else derive_file_id(repository_id, norm)
        )
        # Normalize past_paths entries if provided
        normalized_past: list[str] = []
        if past_paths:
            for p in past_paths:
                try:
                    normalized_past.append(normalize_path(p))
                except ValueError:
                    # Keep raw if can't normalize but not empty
                    if p:
                        normalized_past.append(p)
        return File(
            id=fid,
            repository_id=repository_id,
            path=norm,
            current_path=norm,
            past_paths=normalized_past,
            alive=bool(alive),
            is_test=bool(is_test),
            language=language,
        )

    def with_rename(self, new_path: str, confident: bool = True) -> File:
        """Return a new File reflecting a rename.

        If confident is True, preserves identity: updates current_path/path,
        appends old path to past_paths, keeps same id.
        If confident is False, caller should create a distinct node instead
        — this method raises if confident is False to enforce the rule at call
        site.  For uncertain renames the graph layer must keep distinct nodes.
        """
        if not confident:
            raise ValueError("uncertain rename must keep distinct nodes; create a new File instead")
        new_norm = normalize_path(new_path)
        new_past = list(self.past_paths)
        if self.path not in new_past and self.path != new_norm:
            new_past.append(self.path)
        # Derive new ID for the new path (identity preservation means id stays?
        # According to spec, confident rename preserves File identity with updated
        # current_path and retained historical path.  That implies the stable ID
        # should follow the new path OR remain as originally?  We keep original ID
        # to preserve identity, but also note new path.  However for true stable
        # ID contract File.id = repo:path, the ID would change on rename.
        # We preserve original ID to avoid breaking existing relationships, and
        # store new path as current property.  Caller interprets this as identity
        # preserved.  If strict ID-follows-path is needed, caller should create
        # new node.  Document this behavior.
        return File(
            id=self.id,
            repository_id=self.repository_id,
            path=new_norm,
            current_path=new_norm,
            past_paths=new_past,
            alive=self.alive,
            is_test=self.is_test,
            language=self.language,
        )

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "path": self.path,
            "current_path": self.current_path,
            "past_paths": list(self.past_paths),
            "alive": self.alive,
            "is_test": self.is_test,
            "language": self.language,
        }

    @staticmethod
    def from_record(record: dict[str, Any]) -> File:
        # Support both 'path' and 'current_path' property names
        path_val = record.get("path") or record.get("current_path") or ""
        current = record.get("current_path") or path_val
        return File(
            id=record["id"],
            repository_id=record["repository_id"],
            path=path_val,
            current_path=current,
            past_paths=list(record.get("past_paths") or []),
            alive=bool(record.get("alive", True)),
            is_test=bool(record.get("is_test", False)),
            language=record.get("language"),
        )
