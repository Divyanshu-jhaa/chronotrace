"""Multi-language source discovery with POSIX normalization and exclusion.

Implements ``discover_source_files`` per Iteration 4 spec:
- Uses ``Path.rglob("*.ext")`` per enabled language extension (includes entry files
  ``__init__.py``, ``index.js``, ``index.ts`` etc)
- Normalizes to POSIX via ``relative_to(repo_root).as_posix()`` then
  ``posixpath.normpath`` (matches ingestion canonicalizer)
- Applies glob exclusion via ingestion ``is_excluded`` (gitwildmatch) or
  ``pathspec``/``fnmatch`` fallback, supporting patterns like ``tests/**``,
  ``node_modules/**``, ``.*``, ``__pycache__/**``
- Sorted results and Windows/POSIX identical canonical paths

Keeps ``discover_python_files`` as backward-compatible wrapper.
"""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# POSIX normalization — matches ingestion canonicalizer
# ---------------------------------------------------------------------------

def _normalize_posix(path: str) -> str:
    """Normalize path to POSIX form matching ingestion canonicalizer.

    Delegates to ``chronotrace.ingestion.filtering.normalize_path`` when
    available to guarantee identical results, with pure-posix fallback.
    """
    if path == "":
        raise ValueError("file path must not be empty")
    # Try ingestion canonicalizer first for exact parity
    try:
        from chronotrace.ingestion.filtering import normalize_path as _ingest_norm  # type: ignore

        return _ingest_norm(path)
    except Exception:
        pass
    p = path.replace("\\", "/")
    p = posixpath.normpath(p)
    if p == ".":
        return "."
    if p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p.lstrip("/")
    return p


def _is_excluded(posix_path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    # Primary: use ingestion filtering for exact gitwildmatch semantics
    try:
        from chronotrace.ingestion.filtering import is_excluded as ingestion_is_excluded  # type: ignore

        return ingestion_is_excluded(posix_path, patterns)
    except Exception:
        pass

    # Fallback: try pathspec directly
    try:
        import pathspec  # type: ignore

        spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        if spec.match_file(posix_path):
            return True
    except Exception:
        pass

    # Simple fnmatch fallback with ** and dotfile handling
    import fnmatch

    pp = PurePosixPath(posix_path)
    for pat in patterns:
        pat_norm = pat.replace("\\", "/").strip()
        if not pat_norm:
            continue
        # Explicit dotfile handling for ".*" pattern (hidden files)
        if pat_norm == ".*":
            parts = posix_path.split("/")
            if any(seg.startswith(".") for seg in parts if seg):
                return True
            continue
        if pat_norm == ".*/**" or pat_norm == "**/.*":
            parts = posix_path.split("/")
            if any(seg.startswith(".") for seg in parts if seg):
                return True
            continue
        try:
            if pp.match(pat_norm):
                return True
        except Exception:
            pass
        try:
            if fnmatch.fnmatch(posix_path, pat_norm):
                return True
        except Exception:
            pass
        # Handle prefix pattern like "tests/**" or "node_modules/**"
        if pat_norm.endswith("/**"):
            prefix = pat_norm[:-3]
            # For gitwildmatch, "tests/**" should match "tests/foo" and also
            # ingestion's supplement treats nested "a/tests/foo" as excluded,
            # so we match any segment equals prefix as well.
            if posix_path == prefix or posix_path.startswith(prefix + "/"):
                return True
            # Check if any path segment equals prefix (nested)
            segs = posix_path.split("/")
            if prefix in segs:
                # For patterns like "__pycache__/**" without leading **/
                # match at any depth (ingestion does)
                # For "tests/**" also treat nested as excluded to match ingestion fallback
                idx = segs.index(prefix) if prefix in segs else -1
                if idx != -1:
                    return True
        # Handle "**" patterns heuristic
        if "**" in pat_norm:
            literals = [
                seg for seg in pat_norm.replace("**", "").split("/") if seg and "*" not in seg
            ]
            if literals and all(lit in posix_path for lit in literals):
                if pat_norm.startswith("**/") and pat_norm.endswith("/**"):
                    seg = pat_norm[3:-3]
                    if seg and seg in posix_path:
                        return True
                elif "__pycache__" in pat_norm and "__pycache__" in posix_path:
                    return True
                elif posix_path.endswith(".pyc") and "*.pyc" in pat_norm:
                    return True
                # Generic ** containment already checked via literals all in path
                # For "**/segment" without trailing /** check contains
                if pat_norm.startswith("**/") and not pat_norm.endswith("/**"):
                    seg = pat_norm[3:]
                    if seg and posix_path.endswith(seg):
                        return True
    return False


# ---------------------------------------------------------------------------
# Extension resolution
# ---------------------------------------------------------------------------

_DEFAULT_ENABLED: set[str] = {"python", "javascript", "typescript", "java", "go", "rust"}

def _get_extensions(enabled_languages: set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    """Return set of extensions (with dot) for enabled languages."""
    if enabled_languages is None:
        langs = set(_DEFAULT_ENABLED)
    else:
        # Normalize to lower case strings
        try:
            langs = {
                str(lang_item).lower().strip()
                for lang_item in enabled_languages
                if str(lang_item).strip()
            }
        except Exception:
            langs = set(_DEFAULT_ENABLED)
        if not langs:
            return set()
    # Try to use language.py registry for canonical mapping
    try:
        from .language import EXTENSION_MAP, LANGUAGE_EXTENSIONS  # type: ignore

        exts: set[str] = set()
        for lang in langs:
            if lang in LANGUAGE_EXTENSIONS:
                exts.update(LANGUAGE_EXTENSIONS[lang])
            else:
                # Fallback scan EXTENSION_MAP for lang (in case LANGUAGE_EXTENSIONS incomplete)
                for ext, lang_val in EXTENSION_MAP.items():
                    if lang_val == lang:
                        exts.add(ext)
        if exts:
            return exts
    except Exception:
        pass
    # Hardcoded fallback per spec
    fallback: dict[str, set[str]] = {
        "python": {".py", ".pyi"},
        "javascript": {".js", ".jsx", ".mjs", ".cjs"},
        "typescript": {".ts", ".tsx", ".mts", ".cts"},
        "java": {".java"},
        "go": {".go"},
        "rust": {".rs"},
        "c": {".c", ".h"},
        "cpp": {".cpp", ".cc", ".hpp", ".cxx"},
    }
    ext_set: set[str] = set()
    for lang in langs:
        if lang in fallback:
            ext_set.update(fallback[lang])
    return ext_set


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_source_files(
    repo_root: Path | str,
    exclude_patterns: list[str] | None = None,
    enabled_languages: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[Path]:
    """Recursively discover source files for enabled languages under ``repo_root``.

    - Uses ``Path.rglob("*.ext")`` per extension (includes entry files
      ``__init__.py``, ``index.js``, ``index.ts`` etc)
    - Normalizes each path to POSIX via ``relative_to(repo_root).as_posix()``
      then ``posixpath.normpath`` (via :func:`_normalize_posix`) for
      exclusion matching (handles Windows vs POSIX separators)
    - Applies glob exclusion via ingestion ``is_excluded`` (gitwildmatch) with
      ``pathspec``/``fnmatch`` fallback, supporting patterns like ``tests/**``,
      ``node_modules/**``, ``.*``, ``__pycache__/**``
    - Entry files are not excluded accidentally; exclusion only applies if
      pattern matches canonical path
    - Returns a sorted list of absolute :class:`Path` objects, ordered by
      canonical POSIX path for Windows/POSIX identical results.

    Args:
        repo_root: Repository root directory.
        exclude_patterns: Glob patterns (gitwildmatch) to exclude.
        enabled_languages: Language identifiers (e.g., ``{"python", "javascript"}``).
            Defaults to ``{"python", "javascript", "typescript", "java", "go", "rust"}``.

    Returns:
        Sorted list of absolute :class:`Path` objects matching enabled extensions
        and not excluded.
    """
    patterns = exclude_patterns or []
    repo_root = Path(repo_root)
    if not repo_root.exists() or not repo_root.is_dir():
        return []
    extensions = _get_extensions(enabled_languages)
    if not extensions:
        return []

    # Collect candidates via Path.rglob per extension
    candidates: set[Path] = set()
    for ext in extensions:
        if not ext.startswith("."):
            ext = "." + ext
        # Use rglob "*.ext" per spec
        try:
            # rglob pattern is "*.ext"
            for p in repo_root.rglob(f"*{ext}"):
                # Ensure the file actually ends with this extension (case-insensitive)
                # rglob("*.js") should not match ".jsx" etc, but case differs on Windows
                if not p.name.lower().endswith(ext.lower()):
                    continue
                candidates.add(p)
        except Exception:
            continue

    # Build mapping of Path -> canonical POSIX for sorting and exclusion
    posix_map: dict[Path, str] = {}
    filtered: list[Path] = []
    for p in candidates:
        # Skip if not file
        try:
            if not p.is_file():
                continue
        except Exception:
            continue
        # Compute canonical posix relative path for exclusion check
        try:
            rel = p.relative_to(repo_root)
            posix_rel = _normalize_posix(rel.as_posix())
        except Exception:
            # Fallback if not relative (should not happen via rglob)
            posix_rel = _normalize_posix(p.as_posix())
        if _is_excluded(posix_rel, patterns):
            continue
        posix_map[p] = posix_rel
        filtered.append(p)

    # Sort by canonical POSIX path for deterministic cross-platform ordering,
    # then by string of absolute path as tie-breaker
    filtered.sort(key=lambda path: (posix_map.get(path, path.as_posix()), str(path)))
    return filtered


def discover_python_files(
    repo_root: Path | str,
    exclude_patterns: list[str] | None = None,
) -> list[Path]:
    """Backward-compatible wrapper discovering only Python files.

    Calls :func:`discover_source_files` with ``enabled_languages={"python"}``
    to preserve existing callers.
    """
    return discover_source_files(repo_root, exclude_patterns, enabled_languages={"python"})


def canonical_path(repo_root: Path | str, file_path: Path | str) -> str:
    """Convert absolute ``file_path`` to canonical POSIX-relative path.

    Matches ingestion canonicalizer: ``path.relative_to(repo_root).as_posix()``
    normalized via :func:`_normalize_posix` (which delegates to ingestion
    ``normalize_path`` when available).

    Args:
        repo_root: Repository root.
        file_path: Absolute file path.

    Returns:
        Canonical POSIX-relative path string.
    """
    try:
        rel = Path(file_path).relative_to(Path(repo_root))
        posix = rel.as_posix()
    except (ValueError, Exception):
        posix = Path(file_path).as_posix()
    return _normalize_posix(posix)


__all__ = [
    "discover_source_files",
    "discover_python_files",
    "canonical_path",
]
