"""Language-aware module-to-file mapping.

Handles Python, JavaScript/TypeScript, Java, Go, and fallback for unknown
extensions. Input files are canonical POSIX paths (relative). Produces
deterministic sorted mapping ``specifier -> canonical_path`` per language.

Python:
  pkg/__init__.py -> pkg
  pkg/mod.py -> pkg.mod
  top.py -> top

JavaScript/TypeScript (.js/.ts/.jsx/.tsx/.mjs/.cjs):
  src/index.js -> src
  pkg/sub/index.js -> pkg.sub (also pkg/sub as alias)
  src/utils.js -> src.utils (also src/utils as alias)
  - strips extension, replaces ``/`` with ``.``, handles index fallback

Java (.java):
  com/foo/Bar.java -> com.foo.Bar  (strip .java, ``/`` -> ``.``)

Go (.go):
  pkg/file.go -> pkg/file  (also pkg.file as alias)
  - when ``go.mod`` prefix present, keys are prefixed with module name
  - e.g., with ``module github.com/org/proj`` and file ``pkg/file.go``,
    key becomes ``github.com/org/proj/pkg/file`` (and dot alias)

Fallback (unknown extension):
  path without extension, ``/`` replaced with ``.``
"""

from __future__ import annotations

import posixpath
from pathlib import Path

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(path: str) -> str:
    p = path.replace("\\", "/")
    p = posixpath.normpath(p)
    if p == ".":
        return "."
    if p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p.lstrip("/")
    return p


# ---------------------------------------------------------------------------
# Language detection (inline fallback + optional language.py registry)
# ---------------------------------------------------------------------------

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
    ".go": "go",
}

_JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}


def _detect_language(canon: str) -> str | None:
    """Detect language from canonical path via extension (case-insensitive)."""
    # Try registry first if available
    try:
        from .language import detect_language as _reg_detect

        lang = _reg_detect(canon)
        if lang is not None:
            return lang
    except Exception:
        pass
    _, ext = posixpath.splitext(canon)
    ext = ext.lower()
    if ext in _EXT_TO_LANG:
        return _EXT_TO_LANG[ext]
    return None


# ---------------------------------------------------------------------------
# Go module prefix
# ---------------------------------------------------------------------------

def _read_go_module_prefix(repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    try:
        rp = Path(repo_root)
        # go.mod at repository root
        gomod = rp / "go.mod"
        if gomod.is_file():
            text = gomod.read_text(encoding="utf-8", errors="ignore")
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("//"):
                    continue
                if line.startswith("module "):
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        mod = parts[1].strip().strip('"').strip("'")
                        # strip inline comment after module name
                        mod = mod.split()[0]
                        if mod:
                            return mod
        # Also handle nested go.mod? Search one level? Not needed for MVP
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-language module extraction
# ---------------------------------------------------------------------------

def _python_module(canon: str) -> str | None:
    """Python: pkg/__init__.py -> pkg, pkg/mod.py -> pkg.mod, top.py -> top."""
    low = canon.lower()
    if not low.endswith(".py"):
        return None
    if low == "__init__.py":
        return ""
    if low.endswith("/__init__.py"):
        mod = canon[: -len("/__init__.py")]
        return mod.replace("/", ".")
    # Regular file: strip extension length 3
    mod = canon[: -len(".py")]
    return mod.replace("/", ".")


def _js_module_keys(canon: str) -> list[str]:
    """JavaScript/TypeScript: produce one or two keys (dot and slash alias).

    - src/index.js -> ["src"]
    - pkg/sub/index.js -> ["pkg.sub", "pkg/sub"]
    - src/utils.js -> ["src.utils", "src/utils"]
    """
    _, ext = posixpath.splitext(canon)
    if ext.lower() not in _JS_EXTS:
        return []
    base = canon[: -len(ext)]  # strip extension (preserves case)
    # Detect index fallback
    # basename of base
    basename = posixpath.basename(base)
    if basename.lower() == "index":
        dir_part = posixpath.dirname(base)
        if not dir_part or dir_part == ".":
            # root index.js -> no module name (skip)
            return []
        dot = dir_part.replace("/", ".")
        slash = dir_part
        keys: list[str] = []
        if dot:
            keys.append(dot)
        if slash and slash != dot:
            keys.append(slash)
        return keys
    # Regular file
    dot = base.replace("/", ".")
    slash = base
    keys = []
    if dot:
        keys.append(dot)
    if slash and slash != dot:
        keys.append(slash)
    return keys


def _java_module(canon: str) -> str | None:
    """Java: com/foo/Bar.java -> com.foo.Bar."""
    if not canon.lower().endswith(".java"):
        return None
    # Preserve original case prefix but strip suffix case-insensitively
    # Find suffix length 5
    base = canon[: -len(".java")]
    if not base:
        return None
    return base.replace("/", ".")


def _go_module_keys(canon: str, go_prefix: str | None = None) -> list[str]:
    """Go: pkg/file.go -> ["pkg/file", "pkg.file"]; with prefix also prefixed keys."""
    if not canon.lower().endswith(".go"):
        return []
    base = canon[:-3]  # strip .go (lower check but slice)
    # Handle uppercase .Go: re-derive via splitext
    b, e = posixpath.splitext(canon)
    if e.lower() != ".go":
        return []
    base = b
    if not base or base == ".":
        return []
    dot = base.replace("/", ".")
    slash = base
    keys: list[str] = []
    if go_prefix:
        # Slash-prefixed is canonical Go import path
        slash_prefixed = f"{go_prefix}/{slash}" if slash else go_prefix
        dot_prefixed = slash_prefixed.replace("/", ".")
        keys.append(slash_prefixed)
        if dot_prefixed != slash_prefixed:
            keys.append(dot_prefixed)
        # Also add non-prefixed forms for local resolution / tests without go.mod
        keys.append(slash)
        if dot != slash:
            keys.append(dot)
    else:
        keys.append(slash)
        if dot != slash:
            keys.append(dot)
    # Deduplicate preserving order and filter empties
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _fallback_module(canon: str) -> str | None:
    """Fallback for unknown extensions: strip extension, '/' -> '.'."""
    # Skip well-known config files that should not be import-mapped
    basename = posixpath.basename(canon)
    if basename in {"go.mod", "package.json", "pyproject.toml", "setup.py", "Cargo.toml"}:
        return None
    if not canon or canon == ".":
        return None
    base, _ = posixpath.splitext(canon)
    if not base or base == ".":
        return None
    # If no extension, base == canon; still map
    dot = base.replace("/", ".")
    if not dot:
        return None
    return dot


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_module_map(
    files: list[str] | list[Path],
    repo_root: Path | None = None,
) -> dict[str, str]:
    """Build language-aware ``specifier -> canonical_path`` map.

    Args:
        files: List of canonical POSIX paths (str) or Path objects.
            Paths are normalized to POSIX via ``_normalize``.
        repo_root: Optional repository root for Go ``go.mod`` prefix detection
            and for resolving Path objects relative to root.

    Returns:
        Dict mapping logical module specifiers to canonical file paths.
        The dict is deterministically sorted by key.
    """
    # Normalize input files to canonical POSIX strings
    canons: list[str] = []
    for f in files:
        if isinstance(f, Path):
            if repo_root is not None:
                try:
                    rel = f.relative_to(repo_root).as_posix()
                    canon = _normalize(rel)
                except Exception:
                    canon = _normalize(f.as_posix())
            else:
                canon = _normalize(f.as_posix())
        else:
            canon = _normalize(str(f))
        # Skip empty or "." entries
        if not canon or canon == ".":
            continue
        canons.append(canon)

    # Deterministic: sort canonical list so iteration order is stable regardless of input order
    canons_sorted = sorted(canons)

    # Detect Go prefix once if any .go files present and repo_root provided
    go_prefix: str | None = None
    if repo_root is not None and any(c.lower().endswith(".go") for c in canons_sorted):
        go_prefix = _read_go_module_prefix(repo_root)

    mapping: dict[str, str] = {}

    for canon in canons_sorted:
        lang = _detect_language(canon)

        # Handle explicit langs
        if lang == "python":
            mod = _python_module(canon)
            if mod is None or mod == "":
                continue
            mapping[mod] = canon
        elif lang in ("javascript", "typescript"):
            keys = _js_module_keys(canon)
            for k in keys:
                if k:
                    mapping[k] = canon
        elif lang == "java":
            mod = _java_module(canon)
            if mod is None or mod == "":
                continue
            mapping[mod] = canon
        elif lang == "go":
            keys = _go_module_keys(canon, go_prefix)
            for k in keys:
                if k:
                    mapping[k] = canon
        else:
            # Unknown / fallback – strip extension and replace '/' with '.'
            # Spec: "for unknown, use path without extension with . replaced"
            # We produce dot-only key for strict spec compliance.
            mod = _fallback_module(canon)
            if mod is None or mod == "":
                continue
            mapping[mod] = canon

    # Return deterministic sorted dict (by key)
    return dict(sorted(mapping.items()))


# Compatibility alias used by service
def build_module_map_from_canonical(canonical_paths: list[str]) -> dict[str, str]:
    return build_module_map(canonical_paths, None)
