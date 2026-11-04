"""Language registry — extension mapping, tree-sitter Language + query retrieval.

Provides:
- EXTENSION_MAP: file suffix -> language name (as used by tree-sitter-language-pack)
- LANGUAGE_QUERIES: language -> tree-sitter query string for import-like constructs
- detect_language(Path) -> str | None
- get_language_and_query(path) -> (Language, query_str)

Uses tree_sitter_language_pack when available, with fallback to tree_sitter direct import.
Handles sandboxed cache directories (configures temp dir if default not writable).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Extension map — required exact literal per task 1.3
# ---------------------------------------------------------------------------
EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
}

# ---------------------------------------------------------------------------
# Tree-sitter query strings per language — capture import-like constructs.
# Queries are intentionally simple and tolerant; parser post-processes.
# ---------------------------------------------------------------------------
LANGUAGE_QUERIES: dict[str, str] = {
    "python": """
(import_statement
  name: (dotted_name) @module) @imp
(import_statement
  name: (aliased_import
    name: (dotted_name) @module
    alias: (dotted_name)? @alias)) @imp_alias
(import_from_statement
  module_name: (dotted_name)? @module
  level: (relative_import)? @level) @from
""".strip(),
    "javascript": """
(import_statement
  source: (string) @spec) @imp
(import_clause) @clause
(call_expression
  function: (identifier) @req
  (#eq? @req "require")
  arguments: (arguments (string) @spec)) @require
(export_statement) @export
""".strip(),
    "typescript": """
(import_statement
  source: (string) @spec) @imp
(call_expression
  function: (identifier) @req
  (#eq? @req "require")
  arguments: (arguments (string) @spec)) @require
""".strip(),
    "java": """
(import_declaration) @imp
""".strip(),
    "go": """
(import_declaration) @imp
(import_spec path: (interpreted_string_literal) @spec) @single
""".strip(),
    "rust": """
(use_declaration) @imp
(mod_item) @mod
""".strip(),
    "c": """
(preproc_include
  path: (_) @spec) @imp
""".strip(),
    "cpp": """
(preproc_include
  path: (_) @spec) @imp
""".strip(),
    "ruby": """
(call
  method: (identifier) @req
  (#eq? @req "require")
  arguments: (argument_list (string) @spec)) @imp
(call
  method: (identifier) @req
  (#eq? @req "require_relative")
  arguments: (argument_list (string) @spec)) @imp_rel
""".strip(),
    "php": """
(namespace_use_declaration) @imp
(require_expression) @require
(include_expression) @include
""".strip(),
}

# For alias: javascript covers jsx as same language name; typescript covers tsx.
# Ensure LANGUAGE_QUERIES covers both language names referenced by EXTENSION_MAP.
# All values in EXTENSION_MAP are keys in LANGUAGE_QUERIES.
assert set(EXTENSION_MAP.values()).issubset(set(LANGUAGE_QUERIES.keys())), (
    f"EXTENSION_MAP languages missing in LANGUAGE_QUERIES: "
    f"{set(EXTENSION_MAP.values()) - set(LANGUAGE_QUERIES.keys())}"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_cache_configured() -> None:
    """Attempt to configure tree-sitter-language-pack cache to writable temp dir if needed."""
    try:
        import tree_sitter_language_pack
        from tree_sitter_language_pack.options import PackConfig
    except Exception:
        return
    # Try to see if default cache is writable; if not, redirect to temp
    try:
        # Use get_language for a trivial language? Instead just check cache_dir writability
        cache = tree_sitter_language_pack.cache_dir()
        # cache is .../libs ; parent dir is version dir
        # Attempt to create a temp subdir inside parent to test writability
        # If fails, reconfigure
        test_parent = Path(cache).parent
        # If parent exists and writable, no need to reconfigure
        if test_parent.exists():
            try:
                # try create a probe file in temp fallback? just check access
                probe = test_parent / ".probe_write"
                # sandbox user may not be able to write there
                # attempt to see if we can write
                try:
                    with open(probe, "w") as f:
                        f.write("probe")
                    probe.unlink(missing_ok=True)
                    return  # writable — nothing to do
                except Exception:
                    pass
            except Exception:
                pass
        # Not writable — redirect to tempdir
        tmp_base = Path(tempfile.gettempdir()) / "ts-cache"
        try:
            tmp_base.mkdir(parents=True, exist_ok=True)
        except Exception:
            # fallback to env tmp low
            tmp_base = Path(tempfile.mkdtemp(prefix="ts-cache-"))
        tree_sitter_language_pack.configure(PackConfig(cache_dir=str(tmp_base)))
    except Exception:
        # Silently ignore; get_language will fallback
        pass


_ensure_cache_configured()


def detect_language(path: Path | str) -> str | None:
    """Detect language from file extension via EXTENSION_MAP.

    Args:
        path: file path (Path or str)

    Returns:
        language name (e.g. "python", "javascript") or None if unknown.
        Only returns languages listed in EXTENSION_MAP; other extensions
        return None even if tree_sitter_language_pack would detect them
        (e.g. .txt -> vimdoc), keeping the registry as source of truth.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    # Primary: extension map (fast, deterministic, offline)
    lang = EXTENSION_MAP.get(suffix)
    if lang is not None:
        return lang
    # For unknown extensions, explicitly return None to avoid pack's
    # overly broad detection (e.g. .txt -> vimdoc, .md -> markdown).
    # This keeps behavior stable and matches task 1.3 unit-test expectations
    # where only known extensions should be detected.
    return None


def _get_tree_sitter_language(lang_name: str) -> Any:
    """Retrieve tree_sitter.Language for lang_name via language-pack or fallback.

    Tries tree_sitter_language_pack first; on network/cache failure falls back
    to individual tree_sitter_* wheels (tree_sitter_python etc) which are
    available offline via tree_sitter.Language(...).
    """
    # Primary: tree_sitter_language_pack (may require network for first fetch)
    try:
        from tree_sitter_language_pack import get_language

        return get_language(lang_name)
    except Exception as e_pack:
        # Fallback: individual per-language wheels installed via pip.
        # These expose a `language()` capsule function returning PyCapsule.
        try:
            import importlib

            import tree_sitter  # type: ignore

            fallback_map: dict[str, tuple[str, str | None]] = {
                "python": ("tree_sitter_python", "language"),
                "javascript": ("tree_sitter_javascript", "language"),
                "typescript": ("tree_sitter_typescript", "language_typescript"),
                "java": ("tree_sitter_java", "language"),
                "go": ("tree_sitter_go", "language"),
                "rust": ("tree_sitter_rust", "language"),
                "c": ("tree_sitter_c", "language"),
                "cpp": ("tree_sitter_cpp", "language"),
                "ruby": ("tree_sitter_ruby", "language"),
                "php": ("tree_sitter_php", "language"),
            }
            # Handle jsx/tsx aliases already normalized to javascript/typescript via detect_language
            # So no extra map needed.
            lang_key = lang_name
            entry = fallback_map.get(lang_key)
            if entry is not None:
                mod_name, func_name = entry  # type: ignore[assignment]
                try:
                    mod = importlib.import_module(mod_name)
                    fn = getattr(mod, func_name) if func_name is not None else None  # type: ignore[arg-type]
                    if callable(fn):
                        capsule = fn()
                        # tree_sitter.Language expects capsule
                        return tree_sitter.Language(capsule)  # type: ignore[arg-type]
                    elif fn is not None:
                        return tree_sitter.Language(fn)  # type: ignore[arg-type]
                except Exception:
                    pass
                # TypeScript also supports tsx via separate function; try alternative
                if lang_key == "typescript":
                    try:
                        mod = importlib.import_module("tree_sitter_typescript")
                        for alt in ("language_tsx", "language_tsx", "language"):
                            fn2 = getattr(mod, alt, None)
                            if callable(fn2):
                                try:
                                    return tree_sitter.Language(fn2())  # type: ignore[arg-type]
                                except Exception:
                                    continue
                    except Exception:
                        pass
            # For languages without separate wheel (e.g. php/ruby/rust) we may still
            # propagate original error if no offline fallback available.
        except Exception:
            pass
        # Last resort: raise with original context
        raise RuntimeError(
            f"Unable to load tree_sitter Language for '{lang_name}': {e_pack}"
        ) from e_pack


def get_language(lang_name: str) -> Any:
    """Public alias for _get_tree_sitter_language (for parser compatibility)."""
    return _get_tree_sitter_language(lang_name)


def get_query(lang_name: str) -> str | None:
    return LANGUAGE_QUERIES.get(lang_name)


def get_parser(lang_name: str) -> Any:
    """Return a Parser configured for lang_name (tree_sitter.Parser)."""
    try:
        from tree_sitter import Parser

        lang_obj = _get_tree_sitter_language(lang_name)
        try:
            return Parser(lang_obj)
        except Exception:
            p = Parser()
            p.language = lang_obj  # type: ignore
            return p
    except Exception:
        return None


def get_language_and_query(path: Path | str) -> tuple[Any, str]:
    """Detect language from path and return (Language, query_str).

    Args:
        path: file path

    Returns:
        (Language, query_str) where Language is tree_sitter.Language and query_str is
        the LANGUAGE_QUERIES entry for that language.

    Raises:
        ValueError if language cannot be determined or query missing.
        RuntimeError if Language cannot be loaded.
    """
    lang = detect_language(path)
    if lang is None:
        raise ValueError(f"Cannot detect language for path: {path!r} (suffix {Path(path).suffix!r})")
    query = LANGUAGE_QUERIES.get(lang)
    if query is None:
        raise ValueError(f"No query for language {lang!r} (path {path!r})")
    language_obj = _get_tree_sitter_language(lang)
    return language_obj, query


# Re-export for convenience
# Reverse map: language -> set of extensions (for scanner)
LANGUAGE_EXTENSIONS: dict[str, set[str]] = {}
for _ext, _lang in EXTENSION_MAP.items():
    LANGUAGE_EXTENSIONS.setdefault(_lang, set()).add(_ext)

__all__ = [
    "EXTENSION_MAP",
    "LANGUAGE_QUERIES",
    "LANGUAGE_EXTENSIONS",
    "detect_language",
    "get_language_and_query",
]
