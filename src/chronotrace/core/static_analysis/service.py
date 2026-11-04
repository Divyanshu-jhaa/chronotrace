"""Orchestrator service for static analysis — scan → parse → map → resolve → persist.

Implements ``analyze_repository`` per Iteration 4 design (Decision 5,6,8) and
tasks 8.1-8.3:

- ``analyze_repository(repo_root, repository_id, graph, exclude_patterns,
  enabled_languages) -> AnalysisResult`` orchestrates:
    scan -> ``discover_source_files`` with ``enabled_languages``
    for each file: detect language, ``parse_file`` with language, handle
    tuple/union returns, collect ``parse_failures`` with language
    ``build_module_map`` from canonical paths (language-aware)
    get per-language stdlib sets
    for each parsed file expand multi-name ``RawImports``, resolve via
    ``resolver.resolve_import`` with importer_module derived from canonical
    path per language (python via ``_path_to_module``, js via directory,
    java via package), classify ``filtered_external`` vs ``unresolved`` vs
    ``resolved``, collect edges ``(source_canonical, target_canonical, language)``,
    diagnostics counters (files_scanned/parsed/failed,
    imports_extracted/resolved/unresolved/filtered, per_language dict),
    ``parse_failures[]``, ``unresolved_details[]`` with language, path,
    raw import text, reason
    deduplicate edges sorted, persist via
    ``graph.replace_imports(repository_id, edges)`` or
    ``graph.relationships.batch_imports``
    return ``AnalysisResult`` with diagnostics
- Path normalization via ingestion canonicalizer (posixpath.normpath, replace "\\" with "/")
- 8.2 Windows/POSIX fixture identical edges
- 8.3 diagnostics detail includes language, path, raw import text, reason

``enabled_languages`` defaults to ``{"python","javascript","typescript","java","go"}``
but ``discover_source_files`` can filter.

Path normalization matches ``chronotrace.ingestion.filtering.normalize_path``.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from .mapper import build_module_map
from .models import AnalysisResult, Diagnostics, ParseFailure, RawImport, UnresolvedImport
from .parser import parse_file
from .resolver import get_stdlib_names
from .scanner import canonical_path, discover_python_files


DEFAULT_ENABLED_LANGUAGES: set[str] = {"python", "javascript", "typescript", "java", "go"}

_LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "mts": "typescript",
    "cts": "typescript",
}


def _normalize(path: str) -> str:
    """POSIX normalization matching ingestion canonicalizer.

    Delegates to ``chronotrace.ingestion.filtering.normalize_path`` when
    available for exact parity, with pure-posix fallback. Handles Windows
    separators, redundant ``.``/``//``, leading ``./`` and ``/``.
    """
    if path == "":
        raise ValueError("file path must not be empty")
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


def _detect_language(canonical: str, raw: RawImport | None = None) -> str:
    """Detect language from RawImport.language or file extension.

    Normalizes to: python, javascript, typescript, java, go.
    Defaults to python for unknown extensions.
    """
    if raw is not None:
        try:
            lang_attr = getattr(raw, "language", None)
            if isinstance(lang_attr, str) and lang_attr:
                la = lang_attr.lower().strip()
                if la in _LANGUAGE_ALIASES:
                    return _LANGUAGE_ALIASES[la]
                if la in ("python", "javascript", "typescript", "java", "go", "rust", "c", "cpp"):
                    return la
                # keep as is if known
                if la:
                    return la
        except Exception:
            pass
    # Try language registry first
    try:
        from .language import detect_language as _reg_detect  # type: ignore

        lang = _reg_detect(canonical)
        if lang is not None:
            la = str(lang).lower().strip()
            if la in _LANGUAGE_ALIASES:
                return _LANGUAGE_ALIASES[la]
            return la
    except Exception:
        pass
    lower = canonical.lower()
    if lower.endswith(".py") or lower.endswith(".pyi"):
        return "python"
    if lower.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "javascript"
    if lower.endswith((".ts", ".tsx", ".mts", ".cts")):
        return "typescript"
    if lower.endswith(".java"):
        return "java"
    if lower.endswith(".go"):
        return "go"
    if lower.endswith(".rs"):
        return "rust"
    return "python"


def _get_top_for_service(candidate: str) -> str:
    """Extract top-level for third-party checks (node: + slash + dot)."""
    c = candidate.strip()
    if not c:
        return ""
    if c.startswith("node:"):
        c = c[5:]
    if c.startswith("@"):
        idx = c.find("/")
        if idx != -1:
            return c[:idx]
        dot = c.find(".")
        if dot != -1:
            return c[:dot]
        return c
    # For Go domain imports like github.com/org/lib, keep domain as top
    if "/" in c:
        first = c.split("/")[0]
        # If first contains dot (domain), return as is without dot split
        if "." in first and "/" in candidate:
            return first
        c = first
    if "." in c:
        c = c.split(".")[0]
    return c


def _path_to_module(canonical: str) -> str:
    if not canonical.endswith(".py"):
        return ""
    if canonical == "__init__.py":
        return ""
    if canonical.endswith("/__init__.py"):
        mod = canonical[: -len("/__init__.py")]
        return mod.replace("/", ".")
    mod = canonical[:-3]
    return mod.replace("/", ".")


def _is_init(canonical: str) -> bool:
    return canonical.endswith("__init__.py")


def _importer_info(canonical: str, language: str) -> tuple[str, bool]:
    """Derive importer_module and is_package per language.

    - python: module via _path_to_module, is_package from __init__.py
    - javascript/typescript: canonical path as importer, is_package if index.*
    - java: dotted package (strip .java, "/"->"."), never package
    - go: canonical path
    - others: canonical path
    """
    lang = language.lower().strip()
    if lang == "python":
        return _path_to_module(canonical), _is_init(canonical)
    if lang in ("javascript", "typescript", "js", "ts"):
        base = posixpath.basename(canonical)
        # index.js, index.ts, index.jsx, index.tsx, index.mjs, index.cjs
        is_pkg = base.startswith("index.")
        return canonical, is_pkg
    if lang == "java":
        b, ext = posixpath.splitext(canonical)
        if ext.lower() == ".java":
            pkg = b.replace("/", ".")
            return pkg, False
        return canonical.replace("/", "."), False
    if lang == "go":
        return canonical, False
    # fallback
    return canonical, False


def _raw_text_for(raw: RawImport, single_name: str | None = None) -> str:
    # Try raw_text attribute first
    try:
        txt = getattr(raw, "raw_text", "")
        if isinstance(txt, str) and txt.strip():
            return txt.strip()
    except Exception:
        pass
    try:
        from .parser import _extract_raw_imports  # noqa: F401
    except Exception:
        pass
    level = getattr(raw, "level", 0) or 0
    module = getattr(raw, "module", None)
    name = single_name
    if name is None:
        try:
            name = raw.name  # property returns first name
        except Exception:
            name = None
    if module is None and name is None:
        try:
            names = getattr(raw, "names", [])
            if isinstance(names, list) and names:
                name = names[0]
        except Exception:
            pass
    dots = "." * level if level else ""
    if level == 0:
        if module is not None and name is None:
            base = f"import {module}"
            try:
                asname = raw.asname
                if asname:
                    base += f" as {asname}"
            except Exception:
                pass
            return base
        elif module is not None and name is not None:
            # Could be from import with module and name, but for "import X" case module holds name
            # For absolute import with module and single name expanded, treat as import name
            # If module == name, it's "import X"
            if module == name:
                base = f"import {module}"
                try:
                    asname = raw.asname
                    if asname and asname != name:
                        base += f" as {asname}"
                except Exception:
                    pass
                return base
            return f"from {module} import {name}"
        elif module is None and name is not None:
            base = f"import {name}"
            try:
                asname = raw.asname
                if asname and asname != name:
                    base += f" as {asname}"
            except Exception:
                pass
            return base
        else:
            return "import <unknown>"
    else:
        if module is None and name is not None:
            return f"from {dots} import {name}"
        elif module is not None:
            if name is not None:
                return f"from {dots}{module} import {name}"
            else:
                return f"from {dots}{module} import"
        else:
            return f"from {dots} import {name or ''}"


def _is_third_party(raw_single: RawImport, stdlib: set[str], repo_tops: set[str], language: str | None = None) -> bool:
    """Check if unresolved absolute import is third-party external (not internal missing)."""
    level = getattr(raw_single, "level", 0) or 0
    if level != 0:
        return False
    module = getattr(raw_single, "module", None)
    names = getattr(raw_single, "names", None)
    candidate = None
    if isinstance(module, str) and module.strip():
        candidate = module.strip()
    elif isinstance(names, list) and names:
        candidate = str(names[0]).strip() if names[0] else None
    else:
        try:
            candidate = getattr(raw_single, "name", None)
            if isinstance(candidate, str):
                candidate = candidate.strip()
            else:
                candidate = None
        except Exception:
            candidate = None
    if not candidate:
        return False
    top = _get_top_for_service(candidate)
    if not top:
        return False
    if top in stdlib:
        return False
    if candidate.startswith(("java.", "javax.")):
        return False
    if language in ("javascript", "js", "typescript", "ts"):
        try:
            from .resolver import JS_BUILTINS

            if top in JS_BUILTINS:
                return False
        except Exception:
            pass
    if top in repo_tops:
        return False
    # Also check first slash segment for Go domain (github.com, golang.org)
    try:
        if "/" in candidate:
            first_slash = candidate.split("/")[0].strip()
            if first_slash and first_slash in repo_tops:
                return False
            from .resolver import KNOWN_THIRD_PARTY

            if first_slash in KNOWN_THIRD_PARTY:
                return True
    except Exception:
        pass
    try:
        from .resolver import KNOWN_THIRD_PARTY

        if top in KNOWN_THIRD_PARTY:
            return True
        # Check dotted candidate's first slash domain already handled, but also check candidate prefix
        # For candidate like "github.com/org/lib", top is "github.com" after fix, so above matches
    except Exception:
        return False
    return False


def _resolve_single(
    raw_single: RawImport,
    importer_module: str,
    importer_is_package: bool,
    module_map: dict[str, str],
    stdlib: set[str],
    language: str | None = None,
):
    """Call resolver handling both old and new signatures with language support."""
    try:
        from .resolver import resolve_import  # type: ignore
    except Exception:
        raise
    result: Any = None
    try:
        result = resolve_import(
            raw_single, importer_module, importer_is_package, module_map, stdlib, None, language  # type: ignore[call-arg, call-overload]
        )
    except TypeError:
        try:
            result = resolve_import(  # type: ignore[call-overload, assignment]
                raw_single, importer_module, importer_is_package, module_map, stdlib
            )
        except TypeError:
            try:
                result = resolve_import(  # type: ignore[call-arg, call-overload]
                    raw_single, importer_module, importer_is_package, module_map, stdlib, None
                )
            except Exception:
                raise
    except Exception:
        raise
    if isinstance(result, tuple):
        if len(result) == 2:
            target, reason = result
            if target is not None:
                return target, "resolved", reason
            level = getattr(raw_single, "level", 0) or 0
            try:
                if reason == "beyond_top_level":
                    return None, "unresolved", reason
                if level == 0 and (reason == "stdlib" or reason == "filtered_external"):
                    return None, "filtered_external", reason
                if level != 0 and reason == "filtered_external":
                    return None, "unresolved", reason
            except Exception:
                pass
            reason_str = str(reason)
            if level == 0 and ("stdlib" in reason_str or "filtered" in reason_str):
                return None, "filtered_external", reason
            elif "beyond" in reason_str:
                return None, "unresolved", reason
            else:
                return None, "unresolved", reason
        elif len(result) == 3:
            target, classification, reason = result  # type: ignore
            return target, classification, reason
        else:
            return None, "unresolved", "module_not_found"
    else:
        return None, "unresolved", "module_not_found"


def analyze_repository(
    repo_root: Path,
    repository_id: str,
    graph: Any,
    exclude_patterns: list[str] | None = None,
    enabled_languages: set[str] | None = None,
) -> AnalysisResult:
    """Orchestrate static analysis for a repository snapshot.

    Steps:
      1. Discover source files under repo_root filtered by exclude_patterns
         (POSIX canonicalized). ``enabled_languages`` defaults to
         ``{"python","javascript","typescript","java","go"}`` and is passed to
         ``discover_source_files`` for filtering; when ``None`` the default set
         is used (backward compat for callers that previously discovered only
         Python).
      2. Parse each file with tree-sitter (via ``parse_file``) with explicit
         language; isolate per-file failures (``ERROR`` nodes, ``UnicodeError``,
         ``OSError`` -> ``ParseFailure`` with language). Handles tuple/union
         returns (``ParseResult | ParseFailure | tuple``).
      3. Build language-aware ``module_name -> canonical_path`` map via
         ``build_module_map`` from canonical paths; normalization via ingestion
         canonicalizer (``posixpath.normpath`` + ``\\`` -> ``/``).
      4. Resolve each ``RawImport`` (absolute + relative, stdlib/third-party
         filtering per language). ``importer_module`` derived from canonical
         path per language (python via ``_path_to_module``, js via directory,
         java via package). Per-language stdlib via ``get_stdlib_names``.
         Expands multi-name imports (``from foo import a, b`` -> two singles).
         Classifies ``filtered_external`` vs ``unresolved`` vs ``resolved``.
      5. Collect edges ``(source_canonical, target_canonical, language)``
         deduplicated sorted; persist distinct intra-repo edges via
         ``graph.replace_imports(repository_id, edges)`` or
         ``graph.relationships.batch_imports``. Only ``resolved`` edges are
         persisted; ``filtered_external`` and ``unresolved`` never create File
         nodes or edges.
      6. Return ``AnalysisResult`` with ``Diagnostics`` including
         files_scanned/parsed/failed, imports counts, per_language dict,
         parse_failures[], unresolved_details[] with language, path, raw import
         text, reason.

    Path normalization matches ingestion canonicalizer (POSIX, stripped leading
    slashes) so Windows/POSIX fixtures produce identical edges.
    """
    repo_root = Path(repo_root)
    exclude = exclude_patterns or []

    # Determine effective enabled languages (defaults to 5 per task)
    if enabled_languages is None:
        eff_langs = set(DEFAULT_ENABLED_LANGUAGES)
    else:
        try:
            # Normalize to lower case strings, filter empty
            raw_set = {str(l).lower().strip() for l in enabled_languages if str(l).strip()}
            # Map aliases to canonical
            eff_langs = set()
            for l in raw_set:
                eff_langs.add(_LANGUAGE_ALIASES.get(l, l))
        except Exception:
            eff_langs = set(DEFAULT_ENABLED_LANGUAGES)

    # 1. Scan — discover source files with enabled_languages filter
    discovered: list[Path]
    try:
        from .scanner import discover_source_files as _discover

        discovered = _discover(repo_root, exclude, eff_langs)
    except Exception:
        # Fallback to python-only if scanner fails
        try:
            discovered = discover_python_files(repo_root, exclude)
        except Exception:
            discovered = []

    # Canonical mapping with ingestion normalization
    path_map: dict[Path, str] = {}
    for p in discovered:
        try:
            canon = canonical_path(repo_root, p)
            canon = _normalize(canon)
        except Exception:
            canon = _normalize(p.as_posix())
        path_map[p] = canon

    paired = sorted(path_map.items(), key=lambda kv: kv[1])
    sorted_paths = [k for k, _ in paired]
    sorted_canonicals = [v for _, v in paired]

    files_scanned = len(sorted_paths)

    # 3. Build module map (language-aware) — before resolution, after scan
    try:
        module_map = build_module_map(sorted_canonicals, repo_root)  # type: ignore[call-arg]
    except TypeError:
        try:
            module_map = build_module_map(sorted_canonicals)  # type: ignore[call-arg]
        except Exception:
            module_map = {}
    except Exception:
        module_map = {}

    files_parsed = 0
    files_failed = 0
    imports_extracted = 0
    imports_resolved = 0
    imports_unresolved = 0
    imports_filtered_external = 0
    parse_failures: list[ParseFailure] = []
    unresolved_details: list[UnresolvedImport] = []
    edges: list[tuple[str, str, str]] = []

    # Per-language diagnostics
    per_language: dict[str, dict[str, int]] = {}
    per_language_parse_failures: dict[str, list[ParseFailure]] = {}
    per_language_unresolved: dict[str, list[UnresolvedImport]] = {}

    def _ensure_lang(lang: str) -> None:
        if lang not in per_language:
            per_language[lang] = {
                "files_scanned": 0,
                "files_parsed": 0,
                "files_failed": 0,
                "imports_extracted": 0,
                "imports_resolved": 0,
                "imports_unresolved": 0,
                "imports_filtered_external": 0,
            }
            per_language_parse_failures[lang] = []
            per_language_unresolved[lang] = []

    # Initialize per_language counts for discovered files (files_scanned per language)
    for canon in sorted_canonicals:
        lang = _detect_language(canon)
        lang = _LANGUAGE_ALIASES.get(lang, lang)
        _ensure_lang(lang)
        per_language[lang]["files_scanned"] += 1

    # 2. Parse + 4. Resolve — per file
    for abs_path, canon in zip(sorted_paths, sorted_canonicals):
        lang = _detect_language(canon)
        lang = _LANGUAGE_ALIASES.get(lang, lang)
        _ensure_lang(lang)

        # --- Parse with language, handle tuple/union returns ---
        try:
            result = parse_file(abs_path, lang)  # type: ignore[call-arg]
            # Some parse_file implementations may not accept language kw; fallback handled below
        except TypeError:
            try:
                result = parse_file(abs_path)  # type: ignore[call-arg]
            except Exception as e:
                result = ParseFailure(path=canon, reason=f"{type(e).__name__}: {e}", language=lang)
        except Exception as e:
            result = ParseFailure(path=canon, reason=f"{type(e).__name__}: {e}", language=lang)

        # Normalize result to (imports_list, failure)
        imports_list: list[RawImport] = []
        failure: ParseFailure | None = None

        # Use isinstance checks first (ParseFailure vs ParseResult)
        if isinstance(result, ParseFailure):
            imports_list = []
            failure = result
        elif hasattr(result, "raw_imports") and hasattr(result, "path"):
            # ParseResult-like
            try:
                imports_list = list(getattr(result, "raw_imports", []))
            except Exception:
                imports_list = []
            failure = None
            # Also ensure language on result if present
            if getattr(result, "language", None) is None:
                try:
                    object.__setattr__(result, "language", lang)  # type: ignore
                except Exception:
                    pass
        elif isinstance(result, tuple) and len(result) == 2:
            a, b = result  # type: ignore
            # a could be list[RawImport] or ParseResult, b could be ParseFailure or None
            if isinstance(b, ParseFailure) or b is None:
                if hasattr(a, "raw_imports"):
                    try:
                        imports_list = list(getattr(a, "raw_imports", []))
                    except Exception:
                        imports_list = []
                elif isinstance(a, list):
                    imports_list = list(a)
                elif a is None:
                    imports_list = []
                else:
                    try:
                        imports_list = list(a)  # type: ignore
                    except Exception:
                        imports_list = []
                failure = b
            elif isinstance(a, list) and isinstance(b, ParseFailure):
                imports_list, failure = list(a), b  # type: ignore
            else:
                # Heuristic: if first element is ParseFailure
                if isinstance(a, ParseFailure):
                    imports_list = []
                    failure = a
                elif isinstance(b, ParseFailure):
                    # a is imports
                    imports_list = list(a) if isinstance(a, list) else []
                    failure = b
                else:
                    imports_list = []
                    failure = None
        elif isinstance(result, list):
            # Direct list of RawImports (fallback)
            imports_list = list(result)
            failure = None
        else:
            # Unknown shape — try iterable unpack
            try:
                # Check if result is iterable of 2
                tmp = list(result)  # type: ignore
                if len(tmp) == 2:
                    a, b = tmp
                    if isinstance(b, ParseFailure) or b is None:
                        if isinstance(a, list):
                            imports_list = list(a)
                        elif hasattr(a, "raw_imports"):
                            imports_list = list(getattr(a, "raw_imports", []))
                        else:
                            imports_list = []
                        failure = b
                    else:
                        imports_list = []
                        failure = ParseFailure(path=canon, reason="unknown parse result", language=lang)
                else:
                    imports_list = []
                    failure = ParseFailure(path=canon, reason="unknown parse result", language=lang)
            except Exception:
                imports_list = []
                failure = ParseFailure(path=canon, reason="unknown parse result", language=lang)

        if failure is not None:
            files_failed += 1
            per_language[lang]["files_failed"] += 1
            pf_lang = getattr(failure, "language", None) or lang
            pf_lineno = getattr(failure, "lineno", None)
            pf_reason = getattr(failure, "reason", None) or str(failure)
            if not isinstance(pf_reason, str):
                pf_reason = str(pf_reason)
            pf_raw_text = getattr(failure, "raw_text", None)
            pf_path = canon
            pf = ParseFailure(path=pf_path, reason=pf_reason, lineno=pf_lineno, raw_text=pf_raw_text, language=pf_lang)
            parse_failures.append(pf)
            per_language_parse_failures[lang].append(pf)
            continue

        files_parsed += 1
        per_language[lang]["files_parsed"] += 1

        # Expand multi-name RawImports into single-name entries
        expanded: list[RawImport] = []
        for raw in imports_list:
            try:
                names = getattr(raw, "names", None)
                if isinstance(names, list) and len(names) > 1:
                    asnames = getattr(raw, "asnames", None) or []
                    level = getattr(raw, "level", 0) or 0
                    module = getattr(raw, "module", None)
                    lineno = getattr(raw, "lineno", 1) or 1
                    raw_text = getattr(raw, "raw_text", "") or ""
                    raw_lang = getattr(raw, "language", None) or lang
                    if isinstance(raw_lang, str):
                        raw_lang = _LANGUAGE_ALIASES.get(raw_lang.lower().strip(), raw_lang.lower().strip())
                    else:
                        raw_lang = lang
                    is_rel = getattr(raw, "is_relative", None)
                    if is_rel is None:
                        is_rel = bool(level and int(level) > 0) if raw_lang == "python" else False
                        # For JS, check module starts with .
                        if raw_lang in ("javascript", "typescript"):
                            mod = module or (names[0] if names else "")
                            if isinstance(mod, str) and mod.strip().startswith("."):
                                is_rel = True
                    for idx, single_name in enumerate(names):
                        single_asname = asnames[idx] if idx < len(asnames) else None
                        kwargs: dict[str, Any] = {}
                        if isinstance(raw_lang, str) and raw_lang:
                            kwargs["language"] = raw_lang
                        single_raw = RawImport(
                            module=module,
                            names=[single_name],
                            asnames=[single_asname],
                            level=level,
                            lineno=lineno,
                            raw_text=raw_text,
                            **kwargs,
                        )
                        if not getattr(single_raw, "language", None):
                            try:
                                object.__setattr__(single_raw, "language", raw_lang)  # type: ignore[attr-defined]
                            except Exception:
                                pass
                        # Preserve is_relative if needed
                        try:
                            if is_rel and not getattr(single_raw, "is_relative", False):
                                object.__setattr__(single_raw, "is_relative", is_rel)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        expanded.append(single_raw)
                else:
                    # Ensure language retained
                    if not getattr(raw, "language", None):
                        try:
                            object.__setattr__(raw, "language", lang)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    expanded.append(raw)
            except Exception:
                expanded.append(raw)

        imports_extracted += len(expanded)
        per_language[lang]["imports_extracted"] += len(expanded)

        # Derive importer module per language for resolver
        importer_mod, is_pkg = _importer_info(canon, lang)

        for raw_single in expanded:
            # Determine per-import language (may differ from file if RawImport carries different)
            raw_lang = getattr(raw_single, "language", None) or lang
            if isinstance(raw_lang, str):
                raw_lang = raw_lang.lower().strip()
                raw_lang = _LANGUAGE_ALIASES.get(raw_lang, raw_lang)
            else:
                raw_lang = lang
            if raw_lang not in per_language:
                _ensure_lang(raw_lang)

            single_name = None
            try:
                names = getattr(raw_single, "names", [])
                if isinstance(names, list) and names:
                    single_name = names[0]
                else:
                    single_name = getattr(raw_single, "name", None)
            except Exception:
                pass
            raw_text = _raw_text_for(raw_single, single_name)
            stdlib = get_stdlib_names(raw_lang)
            # Use per-import language for importer info if differs? But importer is file's path, so use file's lang for importer derivation
            # However if raw_single language differs, we should recompute importer for that language? Keep file's importer_mod/is_pkg is fine
            # For correctness, recompute if raw_lang != lang
            use_importer_mod, use_is_pkg = importer_mod, is_pkg
            if raw_lang != lang:
                use_importer_mod, use_is_pkg = _importer_info(canon, raw_lang)

            target, classification, reason = _resolve_single(
                raw_single, use_importer_mod, use_is_pkg, module_map, stdlib, raw_lang
            )
            # Fallback reclassify third-party unresolved as filtered_external using per-language known list
            if classification == "unresolved":
                try:
                    repo_tops = {k.split(".")[0] for k in module_map.keys() if k}
                    # Also handle slash tops for Go/JS? Use _get_top_for_service
                    # Extend repo_tops with slash handling: for keys containing "/", add first segment
                    extra_tops = set()
                    for k in module_map.keys():
                        if "/" in k:
                            extra_tops.add(k.split("/")[0])
                    repo_tops.update(extra_tops)
                    if _is_third_party(raw_single, stdlib, repo_tops, raw_lang):
                        classification = "filtered_external"
                except Exception:
                    pass

            if classification == "resolved" and target is not None:
                imports_resolved += 1
                per_language[raw_lang]["imports_resolved"] += 1
                target_canon = _normalize(target)
                edges.append((canon, target_canon, raw_lang))
            elif classification == "filtered_external":
                imports_filtered_external += 1
                per_language[raw_lang]["imports_filtered_external"] += 1
            elif classification == "unresolved":
                imports_unresolved += 1
                per_language[raw_lang]["imports_unresolved"] += 1
                detail = UnresolvedImport(
                    importer=canon,
                    raw_import=raw_single,
                    reason=str(reason) if reason else "module_not_found",
                    raw_text=raw_text,
                )
                unresolved_details.append(detail)
                per_language_unresolved[raw_lang].append(detail)
            else:
                imports_unresolved += 1
                per_language[raw_lang]["imports_unresolved"] += 1
                detail = UnresolvedImport(
                    importer=canon,
                    raw_import=raw_single,
                    reason=str(reason) if reason else "unknown",
                    raw_text=raw_text,
                )
                unresolved_details.append(detail)
                per_language_unresolved[raw_lang].append(detail)

    # Deduplicate edges sorted — Windows/POSIX identical via canonical POSIX
    seen_edges: set[tuple[str, str, str]] = set()
    deduped: list[tuple[str, str, str]] = []
    for edge in edges:
        # Normalize edge components via _normalize for consistency
        try:
            src_norm = _normalize(edge[0])
            tgt_norm = _normalize(edge[1])
            lang_norm = str(edge[2]).lower().strip() if len(edge) > 2 else "python"
            lang_norm = _LANGUAGE_ALIASES.get(lang_norm, lang_norm)
            norm_edge = (src_norm, tgt_norm, lang_norm)
        except Exception:
            norm_edge = edge  # type: ignore
        if norm_edge not in seen_edges:
            seen_edges.add(norm_edge)
            deduped.append(norm_edge)
    deduped_sorted = sorted(deduped)
    relations_persisted = len(deduped_sorted)

    # Persist via graph — only resolved edges, never for filtered_external/unresolved
    if graph is not None:
        try:
            if hasattr(graph, "replace_imports"):
                # Support both 3-tuple and dict edges — try 3-tuple first
                try:
                    graph.replace_imports(repository_id, deduped_sorted)  # type: ignore[arg-type]
                except TypeError:
                    # Fallback to dict form if driver expects dicts
                    dict_edges = [
                        {"source": s, "target": t, "language": lang} for s, t, lang in deduped_sorted
                    ]
                    graph.replace_imports(repository_id, dict_edges)  # type: ignore[arg-type]
            elif hasattr(graph, "relationships") and hasattr(graph.relationships, "batch_imports"):
                try:
                    from chronotrace.graph.models import derive_file_id

                    id_pairs = []
                    for _edge in deduped_sorted:
                        s = _edge[0] if isinstance(_edge, (list, tuple)) else _edge.get("source", "")  # type: ignore
                        t = _edge[1] if isinstance(_edge, (list, tuple)) else _edge.get("target", "")  # type: ignore
                        id_pairs.append((derive_file_id(repository_id, s), derive_file_id(repository_id, t)))
                    graph.relationships.batch_imports(repository_id, id_pairs)
                except Exception:
                    pass
            else:
                # Try ImportGraphRepository as driver
                try:
                    from chronotrace.core.graph.import_graph import ImportGraphRepository

                    repo = ImportGraphRepository(driver=graph)
                    repo.replace_imports(repository_id, deduped_sorted)  # type: ignore[arg-type]
                except Exception:
                    # Also try graph.graph.repositories import
                    try:
                        from chronotrace.graph.repositories.import_repo import ImportGraphRepository as GraphRepo

                        repo2 = GraphRepo(driver=graph)
                        repo2.replace_imports(repository_id, deduped_sorted)  # type: ignore[arg-type]
                    except Exception:
                        pass
        except Exception:
            raise

    diagnostics = Diagnostics(
        files_scanned=files_scanned,
        files_parsed=files_parsed,
        files_failed=files_failed,
        imports_extracted=imports_extracted,
        imports_resolved=imports_resolved,
        imports_unresolved=imports_unresolved,
        imports_filtered_external=imports_filtered_external,
        parse_failures=list(parse_failures),
        unresolved_details=list(unresolved_details),
        per_language=dict(per_language),
        per_language_counts=dict(per_language),
        per_language_parse_failures=dict(per_language_parse_failures),
        per_language_unresolved=dict(per_language_unresolved),
        per_language_unresolved_details=dict(per_language_unresolved),
    )

    # Also populate legacy per_language_unresolved alias sync via __post_init__, but set explicitly
    analysis_result = AnalysisResult(
        repository_id=repository_id,
        files_scanned=files_scanned,
        files_parsed=files_parsed,
        files_failed=files_failed,
        imports_extracted=imports_extracted,
        imports_resolved=imports_resolved,
        imports_unresolved=imports_unresolved,
        imports_filtered_external=imports_filtered_external,
        relations_persisted=relations_persisted,
        diagnostics=diagnostics,
    )
    return analysis_result  # type: ignore[return-value]
