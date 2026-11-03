"""PyDriller history extraction — chronological traversal with metadata capture."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .exceptions import ExtractionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModificationData:
    """Per-file modification record."""

    path: str
    old_path: str | None
    change_type: str  # ADD, MODIFY, DELETE, RENAME, COPY, UNKNOWN
    added_lines: int | None
    deleted_lines: int | None
    # Preserve original PyDriller type string for debugging
    raw_change_type: str | None = None


@dataclass(frozen=True, slots=True)
class CommitData:
    """Extracted commit with modifications."""

    hash: str
    author_name: str | None
    author_email: str | None
    committer_date: datetime | str | None
    message: str | None
    modifications: list[ModificationData] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    is_merge: bool = False


def _normalize_change_type(raw: Any) -> str:
    """Map PyDriller ModificationType to spec string."""
    if raw is None:
        return "UNKNOWN"
    # Enum with name attribute
    name = getattr(raw, "name", None)
    if name:
        name_up = str(name).upper()
        if name_up in {"ADD", "COPY", "RENAME", "DELETE", "MODIFY", "UNKNOWN"}:
            return name_up
        return name_up
    # String fallback
    s = str(raw).upper()
    if "ADD" in s:
        return "ADD"
    if "DELETE" in s:
        return "DELETE"
    if "RENAME" in s:
        return "RENAME"
    if "COPY" in s:
        return "COPY"
    if "MODIFY" in s:
        return "MODIFY"
    return "UNKNOWN"


def _extract_modifications(commit: Any) -> list[ModificationData]:
    mods: list[ModificationData] = []
    # PyDriller uses modified_files; older versions may use modifications
    raw_files = getattr(commit, "modified_files", None)
    if raw_files is None:
        raw_files = getattr(commit, "modifications", None)
    if raw_files is None:
        raw_files = getattr(commit, "files", []) or []
    # Ensure iterable
    try:
        items = list(raw_files)  # type: ignore[arg-type]
    except Exception:
        items = []

    for mf in items:
        change_type_raw = getattr(mf, "change_type", None)
        change_type = _normalize_change_type(change_type_raw)
        # Path handling: new_path is current path, old_path is previous
        # For DELETE PyDriller may report new_path as truncated or empty;
        # for RENAME/COPY both paths are meaningful; for ADD/MODIFY new_path is authoritative
        new_path = getattr(mf, "new_path", None) or getattr(mf, "filename", None) or getattr(mf, "path", None)
        old_path = getattr(mf, "old_path", None)
        # Handle DELETE: old_path is the full path, new_path may be missing or truncated (e.g., "removed.py" vs "src/removed.py")
        if change_type == "DELETE" and old_path is not None:
            # Use old_path as canonical path for DELETE
            new_path = old_path
        # Fallback for filename vs new_path
        if new_path is None and old_path is not None:
            # Deletion: old_path holds path, new_path may be None
            new_path = old_path
        if new_path is None:
            # Skip entries with no path (should not happen but handle gracefully)
            continue

        # added_lines / deleted_lines — preserve None vs 0
        added = getattr(mf, "added_lines", None)
        deleted = getattr(mf, "deleted_lines", None)
        # Some versions use removed_lines or deletions
        if deleted is None:
            deleted = getattr(mf, "removed_lines", None)
        if deleted is None:
            deleted = getattr(mf, "deleted", None)
        # Normalize: if not int and not None, try to coerce but keep None for unavailable
        if added is not None and not isinstance(added, int):
            try:
                added = int(added)
            except Exception:
                added = None
        if deleted is not None and not isinstance(deleted, int):
            try:
                deleted = int(deleted)
            except Exception:
                deleted = None
        # Preserve None for binary/merge unavailable — don't coerce None to 0
        # Detect binary where diff is not meaningful: return None instead of 0
        # PyDriller returns 0/1 for binary files; spec requires null for binary
        try:
            diff_text = getattr(mf, "diff", "") or ""
            # Heuristic: binary if diff contains Binary marker or is empty, or source_code is None for non-text
            is_binary_diff = "Binary files" in diff_text or diff_text.strip() == ""
            # Check extension for known binary types
            ext = Path(str(new_path)).suffix.lower() if new_path else ""
            binary_exts = {".png",".jpg",".jpeg",".gif",".pdf",".zip",".pyc",".bin",".exe",".dll",".so"}
            is_binary_ext = ext in binary_exts
            # Also check if source_code is None but content exists -> binary
            try:
                src_code = getattr(mf, "source_code", None)
                content = getattr(mf, "content", None)
                is_binary_content = src_code is None and content is not None and len(content) > 0
            except Exception:
                is_binary_content = False
            is_binary = is_binary_diff or is_binary_ext or is_binary_content
            if is_binary:
                # For binary, unknown line stats -> None (preserve None, convert 0/1 to None)
                if added is not None:
                    added = None
                if deleted is not None:
                    deleted = None
        except Exception:
            pass

        mods.append(
            ModificationData(
                path=str(new_path) if new_path is not None else "",
                old_path=str(old_path) if old_path is not None else None,
                change_type=change_type,
                added_lines=added,
                deleted_lines=deleted,
                raw_change_type=str(change_type_raw) if change_type_raw is not None else None,
            )
        )
    return mods


def extract_commits(
    repo_path: str | Path,
    branch: str | None = None,
    from_commit: str | None = None,
    max_commits: int | None = None,
) -> list[CommitData]:
    """Traverse repository chronologically via PyDriller.

    - Uses ``order="date-order"`` for chronological order.
    - Filters by ``only_in_branch`` when branch is provided.
    - Supports ``from_commit`` for incremental ingestion (inclusive; caller
      should deduplicate if exclusive semantics needed).
    - Captures hash, author name/email, committer date, message, per-mod
      path, old_path, change_type, added/deleted lines (None preserved).
    - Handles empty repos, shallow history, malformed metadata gracefully.
    """
    repo_str = str(Path(repo_path).resolve())
    # Graceful handling for non-existent or empty repos
    p = Path(repo_str)
    if not p.exists():
        raise ExtractionError(f"repository path does not exist: {repo_str}", stage="EXTRACTING")

    # Empty repository check: .git exists but no HEAD
    # PyDriller will raise or return empty; we handle both
    try:
        from pydriller import Repository as PyDrillerRepo
    except Exception as e:
        raise ExtractionError(f"PyDriller not available: {e}", stage="EXTRACTING") from e

    kwargs: dict[str, Any] = {
        "path_to_repo": repo_str,
        "order": "date-order",
    }
    if branch:
        kwargs["only_in_branch"] = branch
    if from_commit:
        kwargs["from_commit"] = from_commit

    commits: list[CommitData] = []
    try:
        repo_iter = PyDrillerRepo(**kwargs)
        for commit in repo_iter.traverse_commits():
            try:
                c_hash = getattr(commit, "hash", "") or ""
                c_hash = str(c_hash).strip()
                if not c_hash:
                    continue

                author = getattr(commit, "author", None)
                author_name = None
                author_email = None
                if author is not None:
                    author_name = getattr(author, "name", None)
                    author_email = getattr(author, "email", None)
                    # Normalize empty string to None for missing email
                    if isinstance(author_email, str) and author_email.strip() == "":
                        author_email = None
                    if isinstance(author_name, str) and author_name.strip() == "":
                        author_name = None
                    # Keep None if missing; don't raise
                # Fallback: commit may have author_name/email directly
                if author_name is None:
                    author_name = getattr(commit, "author_name", None)
                if author_email is None:
                    author_email = getattr(commit, "author_email", None)

                # Date: prefer committer_date, fallback to author_date
                committer_date = getattr(commit, "committer_date", None)
                if committer_date is None:
                    committer_date = getattr(commit, "author_date", None)
                # Keep as datetime or string; allow None for malformed

                message = getattr(commit, "msg", None)
                if message is None:
                    message = getattr(commit, "message", None)
                if message is not None:
                    message = str(message)
                    # Empty message is allowed; keep as empty string not None
                    # But treat None specially
                else:
                    message = None

                # Parents and merge detection
                parents = []
                try:
                    raw_parents = getattr(commit, "parents", []) or []
                    parents = [str(p) for p in raw_parents]
                except Exception:
                    parents = []
                is_merge = bool(getattr(commit, "merge", False)) or len(parents) > 1

                modifications = []
                try:
                    modifications = _extract_modifications(commit)
                    # For merge commits, override line stats to None per spec (unknown not zero)
                    if is_merge:
                        for idx, mod in enumerate(modifications):
                            if mod.added_lines == 0 or mod.deleted_lines == 0:
                                modifications[idx] = ModificationData(
                                    path=mod.path,
                                    old_path=mod.old_path,
                                    change_type=mod.change_type,
                                    added_lines=None,
                                    deleted_lines=None,
                                    raw_change_type=mod.raw_change_type,
                                )
                except Exception as em:
                    logger.debug("failed to extract modifications for %s: %s", c_hash, em)
                    modifications = []

                # For merge commits with no files, we still preserve commit but mods may be empty
                # Ensure unknown line stats preserved as None already in modification extraction

                commits.append(
                    CommitData(
                        hash=c_hash,
                        author_name=author_name,
                        author_email=author_email,
                        committer_date=committer_date,
                        message=message,
                        modifications=modifications,
                        parents=parents,
                        is_merge=is_merge,
                    )
                )

                if max_commits is not None and len(commits) >= max_commits:
                    break

            except Exception as inner:
                # Don't fail entire extraction for one malformed commit
                logger.warning("skipping malformed commit metadata: %s", inner)
                continue

    except Exception as e:
        # Empty repo: PyDriller may raise GitCommandError for no commits
        msg = str(e).lower()
        if "no commits" in msg or "empty" in msg or "does not exist" in msg or "not a git repository" in msg or "bad revision" in msg:
            # Gracefully return empty list for empty repos (e.g., fresh repo with no commits or branch not yet created)
            logger.info("repository appears empty or shallow with no traversable commits: %s", e)
            return []
        # For shallow history, pydriller may warn but still return commits; we treat as non-fatal
        raise ExtractionError(f"PyDriller traversal failed: {e}", stage="EXTRACTING") from e

    # Verify chronological order: PyDriller date-order should already be chronological
    # We trust PyDriller; no additional sorting needed

    return commits
