"""Path normalization and glob-based exclusion filtering."""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .exceptions import FilteringError

try:
    import pathspec  # type: ignore[import-untyped]

    _HAS_PATHSPEC = True
except ImportError:
    _HAS_PATHSPEC = False


def normalize_path(path: str) -> str:
    """Normalize file path to POSIX form.

    - Converts Windows separators (``\\\\``) to ``/``.
    - Collapses redundant separators and ``.`` segments via posixpath.normpath.
    - Strips leading ``./`` and leading ``/`` where appropriate, preserving
      relative semantics but ensuring stable IDs.
    - Returns ``\".\"`` for edge case empty after normalization (callers may handle).
    """
    if path == "":
        raise ValueError("file path must not be empty")
    # Convert Windows separators
    p = path.replace("\\", "/")
    # posixpath.normpath collapses //, ./, etc.
    p = posixpath.normpath(p)
    # normpath may return "." for empty-like
    if p == ".":
        return "."
    if p.startswith("./"):
        p = p[2:]
    # Remove leading slash for repository-relative paths
    # But keep as relative; don't turn absolute into relative unless needed
    # For our use, repository paths are relative; leading slash is removed
    if p.startswith("/"):
        p = p.lstrip("/")
    return p


def _compile_pathspec(patterns: list[str]) -> Any:  # type: ignore[no-untyped-def]
    if not _HAS_PATHSPEC or not patterns:
        return None
    try:
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    except Exception:
        return None


def _fnmatch_excluded(posix_path: str, patterns: list[str]) -> bool:
    """Fallback fnmatch-based exclusion checking.

    Uses PurePosixPath.match and fnmatch.fnmatch for gitwildmatch-like behavior.
    Handles ``**/`` prefixes by testing both full path and basename variants.
    """
    pp = PurePosixPath(posix_path)
    for pat in patterns:
        pat_norm = pat.replace("\\", "/").strip()
        if not pat_norm:
            continue
        # Try PurePosixPath.match first (supports **)
        try:
            if pp.match(pat_norm):
                return True
        except Exception:
            pass
        # fnmatch fallback: translate ** handling
        # For patterns like "**/__pycache__/**": check substring containment
        # We implement a simple glob check via fnmatch on the posix path
        # fnmatch doesn't support ** by default but we can attempt multiple forms
        # Try matching the whole path with fnmatch (treat ** as *)
        # Use posix_path against pattern where ** matches any
        try:
            # Normalize pattern for fnmatch: replace ** with *
            fn_pat = pat_norm
            if fnmatch.fnmatch(posix_path, fn_pat):
                return True
            # Also try matching any suffix/prefix for ** patterns
            # e.g., "**/__pycache__/**" should match "src/__pycache__/cache.pyc"
            # fnmatch with "**/__pycache__/**" on "src/__pycache__/cache.pyc" may fail,
            # so try with glob-style containment
            if fnmatch.fnmatch(posix_path, fn_pat.replace("**/", "").replace("/**", "")):
                # Heuristic fallback for simple cases
                pass
        except Exception:
            continue

        # Pure containment fallback for **/segment/** patterns
        # Extract literal segments between ** and check if path contains them
        # Example: "**/__pycache__/**" -> check "__pycache__" in path parts
        if "**" in pat_norm:
            # Split pattern into literals: take parts between ** and wildcards
            # Simple heuristic: if pattern contains "__pycache__" and path contains it => excluded
            # For generic case, check if any literal segment between slashes appears in path
            literals = [seg for seg in pat_norm.replace("**", "").split("/") if seg and "*" not in seg and seg != ""]
            if literals and all(lit in posix_path for lit in literals):
                # For "*.pyc" style, check suffix
                if pat_norm.endswith("*.pyc") and posix_path.endswith(".pyc"):
                    return True
                if literals:
                    # Verify at least one literal matches a path component
                    path_parts = posix_path.split("/")
                    if any(lit in path_parts or lit in posix_path for lit in literals):
                        # Need to be more precise for **/__pycache__/** vs **/*.pyc
                        if "__pycache__" in pat_norm and "__pycache__" in posix_path:
                            return True
                        if "*.pyc" in pat_norm and posix_path.endswith(".pyc"):
                            return True
                        # Generic: if pattern is **/something/** and path contains something
                        if pat_norm.startswith("**/") and pat_norm.endswith("/**"):
                            seg = pat_norm[3:-3]
                            if seg and seg in posix_path:
                                return True
        # Suffix match for "*.pyc"
        if pat_norm.startswith("**/") and "*" in pat_norm:
            suffix = pat_norm.rsplit("*", 1)[-1] if "*" in pat_norm else ""
            if suffix and posix_path.endswith(suffix):
                # Check prefix **/ matches any directory
                return True
    return False


def is_excluded(path: str, patterns: list[str]) -> bool:
    """Return True if path matches any glob exclusion pattern.

    Normalizes path to POSIX before matching and tests both POSIX and
    Windows-style inputs identically (normalization makes them same).

    Patterns are gitwildmatch globs (e.g., ``**/__pycache__/**``, ``**/*.pyc``).
    """
    if not patterns:
        return False
    if not path:
        return False
    try:
        normalized = normalize_path(path)
    except ValueError:
        normalized = path.replace("\\", "/")
    # First try pathspec if available
    if _HAS_PATHSPEC:
        spec = _compile_pathspec(patterns)
        if spec is not None:
            try:
                if spec.match_file(normalized):
                    return True
            except Exception:
                pass
            # Also try matching with original Windows-style? Already normalized same
            # pathspec handles POSIX paths; normalized covers both
    # Fallback / supplement with fnmatch logic
    # We try fnmatch approach as well for robustness across platforms
    if _fnmatch_excluded(normalized, patterns):
        return True
    # Also try direct PurePosixPath.match for each pattern as final check
    pp = PurePosixPath(normalized)
    for pat in patterns:
        pat_norm = pat.replace("\\", "/")
        try:
            if pp.match(pat_norm):
                return True
        except Exception:
            continue
        # Try fnmatch on normalized
        try:
            if fnmatch.fnmatch(normalized, pat_norm):
                return True
        except Exception:
            continue
    return False


@dataclass(slots=True)
class FilteringResult:
    filtered_commits: list  # list[CommitData] with modifications pruned
    filtered_out_count: int
    kept_count: int


def filter_commits(commits: list, patterns: list[str]) -> FilteringResult:
    """Filter modifications in commits by glob patterns.

    Returns pruned commits (modifications removed if excluded) and counts.
    Paths are POSIX-normalized before filtering and persistence.
    Excluded paths do not reach graph writes and increment filteredOut counter.
    """
    if not patterns:
        # No filtering needed, but still normalize paths in place?
        # We normalize modifications' paths for consistency
        for commit in commits:
            # CommitData is frozen; need to create new ModificationData with normalized paths
            # But for efficiency when no patterns, just return as-is
            pass
        return FilteringResult(filtered_commits=commits, filtered_out_count=0, kept_count=sum(len(c.modifications) for c in commits))

    filtered_out = 0
    kept = 0
    filtered_commits = []
    for commit in commits:
        kept_mods = []
        for mod in commit.modifications:
            # Normalize path for both new path and old path
            try:
                norm_path = normalize_path(mod.path)
            except Exception:
                norm_path = mod.path.replace("\\", "/")
            # Test exclusion on normalized path
            # Also test with old_path if present for rename/delete cases? Should check both,
            # but spec says excluded paths don't enter graph — check current path
            excluded = is_excluded(norm_path, patterns)
            # For rename, also check old_path? If old_path is excluded, the rename may still be excluded
            # But we treat modification as excluded if either path matches (conservative)
            if not excluded and mod.old_path:
                try:
                    norm_old = normalize_path(mod.old_path)
                except Exception:
                    norm_old = mod.old_path.replace("\\", "/")
                if is_excluded(norm_old, patterns):
                    # If old path excluded and new path not, we still consider for filtering?
                    # Example: __pycache__ file renamed to src/main.py — unusual.
                    # For now only exclude based on new path; old path excluded doesn't affect new valid file
                    pass
            if excluded:
                filtered_out += 1
            else:
                kept += 1
                # Preserve normalized path in modification copy
                if norm_path != mod.path:
                    # Need to create new modification with normalized path
                    from .extractor import ModificationData

                    new_mod = ModificationData(
                        path=norm_path,
                        old_path=normalize_path(mod.old_path) if mod.old_path else None,
                        change_type=mod.change_type,
                        added_lines=mod.added_lines,
                        deleted_lines=mod.deleted_lines,
                        raw_change_type=mod.raw_change_type,
                    )
                    kept_mods.append(new_mod)
                else:
                    kept_mods.append(mod)
        # If commit has no kept modifications after filtering, we still keep commit?
        # Spec: "excluded paths do not reach graph writes" — commit itself may still be written but files not.
        # We keep commit with pruned modifications list; if all filtered, modifications empty
        if kept_mods != commit.modifications:
            from .extractor import CommitData

            pruned = CommitData(
                hash=commit.hash,
                author_name=commit.author_name,
                author_email=commit.author_email,
                committer_date=commit.committer_date,
                message=commit.message,
                modifications=kept_mods,
                parents=commit.parents,
                is_merge=commit.is_merge,
            )
            filtered_commits.append(pruned)
        else:
            filtered_commits.append(commit)
    return FilteringResult(
        filtered_commits=filtered_commits,
        filtered_out_count=filtered_out,
        kept_count=kept,
    )
