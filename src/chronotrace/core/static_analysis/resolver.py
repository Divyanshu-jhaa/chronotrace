"""Import resolution — map ``RawImport`` to repository file path.

Resolves absolute and relative imports via a repository-scoped
``module_name -> canonical POSIX path`` map, with stdlib filtering
and ``beyond_top_level`` detection.

Contract
--------
``resolve_import(raw, importer_module, importer_is_package, module_map, stdlib_names)``
returns ``(resolved_path_or_None, reason)`` where ``reason`` is one of
``resolved``, ``beyond_top_level``, ``module_not_found``, ``stdlib`` /
``filtered_external`` (stdlib) etc.

* Absolute (``level == 0``) — look up ``raw.module`` (real name, not alias)
  directly in ``module_map`` with ``__init__.py`` awareness (mapper already
  maps ``pkg -> pkg/__init__.py``).  If not found, check stdlib set via top-level
  segment, otherwise ``module_not_found``.
* Relative (``level > 0``) — compute importer package (``importer_module`` if
  ``importer_is_package`` else its parent), walk ``level-1`` parents, detect
  ``beyond_top_level``, build candidate (``module`` if present else first
  imported name), then same map/stdlib lookup.
* Alias handling — always uses ``raw.module`` / ``raw.names`` real names,
  never ``raw.asnames``.

Multi-language branching
------------------------
``RawImport.language`` drives branching (``python`` default):

* ``python`` — level arithmetic as before
* ``javascript``/``typescript`` — ``./``/``../`` relative via importer dir +
  specifier normalized, extension ``.js/.ts/.jsx/.tsx`` and ``index.*``
  fallback, bare specifier absolute lookup in ``module_map``
* ``java`` — bare package name direct lookup, no relative
* ``go`` — bare quoted import path direct lookup
* unknown — absolute lookup

Per-language filtering
----------------------
Filtering is central: if candidate top-level in stdlib set for that language
-> ``filtered_external``, else if top not in module_map and known third-party
list -> ``filtered_external``, else ``unresolved``.

* Python: ``sys.stdlib_module_names`` (fallback hard-coded set)
* JavaScript/TypeScript: Node.js builtins (``fs``, ``path``, ``http``, ...)
* Java: ``java.*``, ``javax.*``, ``jdk.*`` prefixes / tops ``java``, ``javax``
* Go: Go stdlib manifest (``fmt``, ``os``, ``net``, ``io``, ...)
"""

from __future__ import annotations

import posixpath
import sys
from typing import overload

from .models import RawImport

# ---------------------------------------------------------------------------
# Fallback constants
# ---------------------------------------------------------------------------

_PYTHON_FALLBACK: set[str] = {
    "abc",
    "ast",
    "asyncio",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "hashlib",
    "importlib",
    "inspect",
    "io",
    "json",
    "logging",
    "math",
    "os",
    "pathlib",
    "re",
    "sys",
    "typing",
    "unittest",
    "urllib",
    "uuid",
}

JS_BUILTINS: set[str] = {
    "fs",
    "path",
    "http",
    "https",
    "os",
    "util",
    "events",
    "stream",
    "crypto",
    "buffer",
    "child_process",
    "cluster",
    "dgram",
    "dns",
    "domain",
    "assert",
    "constants",
    "module",
    "net",
    "punycode",
    "querystring",
    "readline",
    "repl",
    "string_decoder",
    "timers",
    "tls",
    "tty",
    "url",
    "v8",
    "vm",
    "zlib",
    "process",
    "perf_hooks",
    "worker_threads",
    "inspector",
    "async_hooks",
    "http2",
    "wasi",
    "trace_events",
    "diagnostics_channel",
    "fs/promises",
}

GO_STDLIB: set[str] = {
    "bufio",
    "bytes",
    "compress",
    "container",
    "context",
    "crypto",
    "database",
    "debug",
    "embed",
    "encoding",
    "errors",
    "expvar",
    "flag",
    "fmt",
    "go",
    "hash",
    "html",
    "image",
    "index",
    "io",
    "log",
    "math",
    "mime",
    "net",
    "os",
    "path",
    "plugin",
    "reflect",
    "regexp",
    "runtime",
    "sort",
    "strconv",
    "strings",
    "sync",
    "syscall",
    "testing",
    "text",
    "time",
    "unicode",
    "unsafe",
    "internal",
    "arena",
    "cmp",
    "maps",
    "slices",
    "unique",
    "iter",
    "structs",
    "filepath",
}

JAVA_STDLIB_TOPS: set[str] = {
    "java",
    "javax",
    "jdk",
    "sun",
    "com.sun",
    "org.w3c",
    "org.xml",
}

JAVA_STDLIB_PREFIXES: tuple[str, ...] = (
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "com.sun.",
    "org.w3c.",
    "org.xml.",
)

KNOWN_THIRD_PARTY: set[str] = {
    # Python
    "requests",
    "neo4j",
    "typer",
    "fastapi",
    "uvicorn",
    "pydantic",
    "httpx",
    "pyyaml",
    "yaml",
    "rich",
    "pydriller",
    "dotenv",
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "sklearn",
    "django",
    "flask",
    "sqlalchemy",
    "redis",
    "celery",
    "boto3",
    "pytest",
    "coverage",
    "mypy",
    "ruff",
    # JavaScript / TypeScript (npm)
    "react",
    "react-dom",
    "react-router",
    "lodash",
    "express",
    "vue",
    "angular",
    "next",
    "nuxt",
    "svelte",
    "axios",
    "webpack",
    "babel",
    "jest",
    "mocha",
    "chai",
    "typescript",
    "eslint",
    "prettier",
    "moment",
    "jquery",
    "d3",
    "three",
    "socket.io",
    "redux",
    "underscore",
    "backbone",
    "ember",
    "knockout",
    "dayjs",
    "ramda",
    "immutable",
    "rxjs",
    "vite",
    "rollup",
    "parcel",
    "styled-components",
    "tailwindcss",
    "bootstrap",
    "material-ui",
    "antd",
    "chart.js",
    # Go / generic github
    "github.com",
    "golang.org",
    # Java third-party (popular)
    "org.springframework",
    "com.google",
    "org.apache",
    "io.micronaut",
    "com.fasterxml",
}


def _get_top_level(candidate: str) -> str:
    """Extract top-level specifier for stdlib / third-party checks.

    Handles:
    - ``node:fs`` -> ``fs``
    - ``lodash/fp`` -> ``lodash``
    - ``@babel/core`` -> ``@babel`` (scoped)
    - ``java.util.List`` -> ``java``
    - ``net/http`` (Go) -> ``net``
    - ``os.path`` (Python) -> ``os``
    """
    c = candidate.strip()
    if not c:
        return ""
    if c.startswith("node:"):
        c = c[5:]
    # Scoped npm packages like @babel/core -> keep scope part as top
    if c.startswith("@"):
        slash_idx = c.find("/")
        if slash_idx != -1:
            return c[:slash_idx]
        dot_idx = c.find(".")
        if dot_idx != -1:
            return c[:dot_idx]
        return c
    # General slash handling (Go, JS subpath)
    if "/" in c:
        c = c.split("/")[0]
    if "." in c:
        c = c.split(".")[0]
    return c


def _is_stdlib_candidate(candidate: str, stdlib_names: set[str], language: str | None) -> bool:
    """Return True if candidate should be considered stdlib for filtering."""
    if not candidate:
        return False
    top = _get_top_level(candidate)
    if top and top in stdlib_names:
        return True
    # Language-specific fallback using built-in sets (covers callers that passed generic set)
    if language in ("javascript", "js", "typescript", "ts"):
        if top in JS_BUILTINS:
            return True
    elif language == "java":
        if candidate.startswith(JAVA_STDLIB_PREFIXES):
            return True
        if top in JAVA_STDLIB_TOPS:
            return True
    elif language == "go":
        if top in GO_STDLIB:
            return True
    # Generic fallback for Java prefixes even when language not specified
    if candidate.startswith(("java.", "javax.")):
        return True
    return False


def get_stdlib_names(language: str | None = None) -> set[str]:
    """Return stdlib top-level names per language.

    * ``python`` / None -> ``sys.stdlib_module_names`` with fallback
    * ``javascript``/``js``/``typescript``/``ts`` -> Node.js builtins
    * ``java`` -> Java stdlib tops (java, javax, jdk, sun...)
    * ``go`` -> Go stdlib manifest
    """
    if language in ("javascript", "js", "typescript", "ts"):
        return set(JS_BUILTINS)
    if language == "java":
        return set(JAVA_STDLIB_TOPS)
    if language == "go":
        return set(GO_STDLIB)
    # Python (default) - also handle explicit "python"
    try:
        names = getattr(sys, "stdlib_module_names", None)
        if names is not None:
            return set(names)
    except Exception:
        pass
    return set(_PYTHON_FALLBACK)


def get_stdlib_for_language(lang: str | None) -> set[str]:
    """Alias per spec: per-language stdlib set."""
    return get_stdlib_names(lang)


class _ReasonStr(str):
    """String that compares equal to any of its aliases."""

    def __new__(cls, value: str, aliases: set[str] | None = None) -> _ReasonStr:
        obj = super().__new__(cls, value)
        a: set[str] = set(aliases) if aliases else set()
        a.add(value)
        obj._aliases = a  # type: ignore[attr-defined]
        return obj  # type: ignore[return-value]

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, str):
            if other in self._aliases:  # type: ignore[attr-defined]
                return True
            return super().__eq__(other)
        return super().__eq__(other)  # type: ignore[no-any-return]

    def __hash__(self) -> int:  # type: ignore[override]
        return super().__hash__()

    def __contains__(self, sub: object) -> bool:  # type: ignore[override]
        if isinstance(sub, str):
            if super().__contains__(sub):
                return True
            for alias in self._aliases:  # type: ignore[attr-defined]
                if sub in alias:
                    return True
            return False
        return super().__contains__(sub)  # type: ignore[no-any-return, operator]


def _lookup_candidate(candidate: str, module_map: dict[str, str]) -> str | None:
    if candidate in module_map:
        return module_map[candidate]
    alt = candidate + ".__init__"
    if alt in module_map:
        return module_map[alt]
    return None


def _strip_quotes(s: str) -> str:
    """Strip surrounding quotes/backticks and whitespace."""
    if not isinstance(s, str):
        return str(s)
    # First strip whitespace
    v = s.strip()
    # Strip surrounding matching quotes repeatedly
    # Handle `"fmt"`, `'fmt'`, `` `fmt` ``, `"'fmt'"` etc
    for _ in range(3):
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'", "`"):
            v = v[1:-1].strip()
        else:
            break
    # Also strip stray leading/trailing quotes without matching
    v = v.strip().strip('"').strip("'").strip("`").strip()
    return v


def _extract_specifier(raw: RawImport) -> str | None:
    """Extract specifier string from RawImport (real name, not alias)."""
    # Prefer module
    mod = getattr(raw, "module", None)
    if isinstance(mod, str) and mod.strip() != "":
        cleaned = _strip_quotes(mod)
        if cleaned:
            return cleaned
    # Fallback to names[0]
    names = getattr(raw, "names", None)
    if isinstance(names, list) and names:
        first = names[0]
        if isinstance(first, str) and first.strip():
            cleaned = _strip_quotes(first)
            if cleaned:
                return cleaned
    # Also check singular name property
    try:
        name_prop = getattr(raw, "name", None)
        if isinstance(name_prop, str) and name_prop.strip():
            cleaned = _strip_quotes(name_prop)
            if cleaned:
                return cleaned
    except Exception:
        pass
    return None


def _get_language(raw: RawImport, explicit: str | None = None) -> str:
    """Determine language for RawImport, defaulting to python."""
    if explicit and isinstance(explicit, str) and explicit.strip():
        lang = explicit.strip().lower()
        if lang in ("js", "jsx"):
            return "javascript"
        if lang in ("ts", "tsx"):
            return "typescript"
        return lang
    try:
        lang_attr = getattr(raw, "language", None)
        if isinstance(lang_attr, str) and lang_attr.strip():
            lang = lang_attr.strip().lower()
            if lang in ("js", "jsx"):
                return "javascript"
            if lang in ("ts", "tsx"):
                return "typescript"
            return lang
    except Exception:
        pass
    # Also check raw_text? Not needed
    return "python"


def _derive_importer_dir(importer_module: str | None, importer_is_package: bool) -> str:
    """Derive POSIX directory for JS relative resolution.

    Handles both POSIX path (``src/app.js``) and dotted module (``src.app``)
    forms. For file importers returns dirname, for package importers returns
    the package path itself.
    """
    if not importer_module:
        return ""
    mod = importer_module.strip()
    if not mod:
        return ""
    mod_posix = mod.replace("\\", "/")
    # If contains slash -> treat as path
    if "/" in mod_posix:
        # Strip leading/trailing slashes for dirname logic but keep structure
        # Use posixpath to compute dirname
        if importer_is_package:
            # If is_package, importer is a directory/package itself
            # Check if path has file extension -> still dirname, else itself
            ext = posixpath.splitext(mod_posix)[1]
            if ext.lower() in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".py", ".java", ".go"):
                return posixpath.dirname(mod_posix)
            else:
                return mod_posix.rstrip("/")
        else:
            return posixpath.dirname(mod_posix)
    else:
        # No slash: maybe dotted name or bare filename
        if "." in mod_posix:
            # Could be dotted Python/Java module or filename with extension
            # If contains file extension at end, handle as file
            # Check if last segment looks like extension? e.g., "app.js" has dot but no slash
            # For case "app.js" (single file with extension at root), dirname is ""
            # For "pkg.sub" (dotted package) -> path "pkg/sub"
            # Heuristic: if extension is known JS/TS/Python etc, treat as file
            last = mod_posix.rsplit(".", 1)
            ext = "." + last[1] if len(last) == 2 else ""
            if ext.lower() in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".py", ".java", ".go"):
                # File with extension at root or dotted? For "src.app.js"?? but slash case already handled
                # For single file "app.js" -> dirname ""
                # For "pkg.mod.js" is ambiguous: treat as file path "pkg/mod.js" -> dirname "pkg"
                path = mod_posix.replace(".", "/")
                # Need to reconstruct: "app.js" -> "app.js" is not path, but our replace would make "app/js"
                # So avoid replace for file extension case
                # Better: if original had dot but no slash and ext is known, then it's filename at root
                # So dirname is "" for single file, or package dir for nested?
                # Example "pkg.a" is not file, ext ".a" not known, so fall through to dotted path logic
                # So only for known ext files with single dot -> root file
                if mod_posix.count(".") == 1:
                    return ""
                else:
                    # Dotted with file extension: e.g., "pkg.sub.js"? Not common, but treat as path
                    without_ext = mod_posix[: -len(ext)]
                    path = without_ext.replace(".", "/")
                    return posixpath.dirname(posixpath.join(path, "file" + ext))  # simplifies to path
            # Dotted module name -> convert to path
            path = mod_posix.replace(".", "/")
            if importer_is_package:
                return path
            else:
                return posixpath.dirname(path)
        else:
            # Single segment like "main", "a"
            return ""


def _js_try_relative_lookup(normalized_base: str, module_map: dict[str, str]) -> str | None:
    """Try JS relative candidate with extensions and index fallback."""
    # Candidates in priority order
    candidates: list[str] = []
    # bare normalized
    candidates.append(normalized_base)
    # with extensions
    for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        candidates.append(normalized_base + ext)
    # index fallback
    for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        candidates.append(posixpath.join(normalized_base, "index" + ext))
        # Also handle directory index without join? posixpath.join handles
    # Also try dot slash variations
    for cand in list(candidates):
        # Also try without leading ./
        if cand.startswith("./"):
            candidates.append(cand[2:])

    for cand in candidates:
        if cand in module_map:
            return module_map[cand]
        norm = posixpath.normpath(cand)
        if norm in module_map:
            return module_map[norm]
        # Also try candidate without extension stripped? e.g., map key "src/utils" vs candidate "src/utils.js"
        # The above covers both directions: if map has "src/utils" and candidate is "src/utils.js", not found.
        # But we also want to match "src/utils" key when candidate is "src/utils.js" stripped?
        # Try stripping extension from candidate
        # If candidate ends with known ext, try without ext
        for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
            if cand.endswith(ext):
                without = cand[: -len(ext)]
                if without in module_map:
                    return module_map[without]
                norm_without = posixpath.normpath(without)
                if norm_without in module_map:
                    return module_map[norm_without]
    return None


def _js_lookup_bare(candidate: str, module_map: dict[str, str]) -> str | None:
    """Bare specifier lookup for JS (absolute). Mirrors relative but without join."""
    cand = candidate.strip()
    if not cand:
        return None
    # Direct
    if cand in module_map:
        return module_map[cand]
    norm = posixpath.normpath(cand)
    if norm in module_map:
        return module_map[norm]
    # Try with extensions (for bare like "pkg/sub" -> maybe file "pkg/sub.js")
    for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        with_ext = cand + ext
        if with_ext in module_map:
            return module_map[with_ext]
        norm_ext = posixpath.normpath(with_ext)
        if norm_ext in module_map:
            return module_map[norm_ext]
    # Index fallback: package entry files
    for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        idx = posixpath.join(cand, "index" + ext)
        if idx in module_map:
            return module_map[idx]
        norm_idx = posixpath.normpath(idx)
        if norm_idx in module_map:
            return module_map[norm_idx]
    # Also try candidate as is but with posixpath handling of ./ prefix
    # Check stripped node: prefix? Not needed
    return None


@overload
def resolve_import(
    raw: RawImport,
    importer_module: str | None,
    importer_is_package: bool,
    module_map: dict[str, str],
    stdlib_names: set[str],
) -> tuple[str | None, str]: ...


@overload
def resolve_import(
    raw: RawImport,
    importer_module: str | None,
    importer_is_package: bool,
    module_map: dict[str, str],
    stdlib_names: set[str],
    repo_tops: set[str] | None,
) -> tuple[str | None, str, str]: ...


@overload
def resolve_import(
    raw: RawImport,
    importer_module: str | None,
    importer_is_package: bool,
    module_map: dict[str, str],
    stdlib_names: set[str],
    repo_tops: set[str] | None,
    language: str | None,
) -> tuple[str | None, str, str]: ...


def resolve_import(  # type: ignore[no-redef]
    raw: RawImport,
    importer_module: str | None,
    importer_is_package: bool,
    module_map: dict[str, str],
    stdlib_names: set[str],
    repo_tops: set[str] | None = None,
    *extra: object,
    **kw: object,
) -> tuple[str | None, str] | tuple[str | None, str, str]:
    """Resolve a single ``RawImport`` to a repository file path.

    Supports both the 5-arg task contract (returns 2-tuple) and the
    6-arg service contract (returns 3-tuple with classification), plus
    optional ``language`` for per-language stdlib handling.

    Central filtering logic (in order):
    1. If resolved in ``module_map`` -> ``resolved``
    2. Else if candidate top-level in stdlib set for that language -> ``filtered_external``
    3. Else if top not in ``module_map`` and in known third-party list -> ``filtered_external``
    4. Else -> ``unresolved`` (with ``beyond_top_level`` / ``module_not_found``)

    For Java, ``java.*``/``javax.*`` prefixes are treated as stdlib even when
    ``language`` not specified.

    ``repo_tops`` is accepted but not required for correctness — stdlib filtering
    already uses ``stdlib_names`` and module_map lookup. When provided, it helps
    distinguish internal missing (top in repo) vs third-party external.
    ``language`` may be supplied via keyword or via ``raw.language`` attribute.
    """
    # --- language & repo_tops extraction ---------------------------------
    language: str | None = None
    if "language" in kw:
        language = kw["language"]  # type: ignore[assignment]
    elif "lang" in kw:
        language = kw["lang"]  # type: ignore[assignment]

    # Also try to infer language from raw attribute if not supplied
    if language is None:
        try:
            lang_attr = getattr(raw, "language", None)
            if isinstance(lang_attr, str) and lang_attr:
                language = lang_attr
        except Exception:
            pass
    # Handle extra positional args for legacy calls
    if extra:
        if repo_tops is None:
            possible = extra[0]
            if isinstance(possible, set) or possible is None:
                repo_tops = possible  # type: ignore[assignment]
                if len(extra) >= 2 and isinstance(extra[1], str):
                    if language is None:
                        language = extra[1]
            elif isinstance(possible, str):
                if language is None and possible in ("python", "javascript", "js", "typescript", "ts", "java", "go"):
                    language = possible
                else:
                    if language is None:
                        language = possible
        else:
            if language is None:
                for item in extra:
                    if isinstance(item, str) and item in ("python", "javascript", "js", "typescript", "ts", "java", "go"):
                        language = item
                        break

    if "repo_tops" in kw:
        repo_tops = kw["repo_tops"]  # type: ignore[assignment]

    expects_three = False
    if repo_tops is not None or extra or kw:
        if repo_tops is not None:
            expects_three = True
        elif extra:
            expects_three = True
        elif "repo_tops" in kw:
            expects_three = True

    # Defensive defaults
    if stdlib_names is None:
        stdlib_names = set()
    if module_map is None:
        module_map = {}

    importer_module = importer_module or ""

    # Normalize language
    lang_norm = _get_language(raw, language)

    # -------------------------------------------------------------------
    # Branch per language
    # -------------------------------------------------------------------
    # Python branch keeps original level arithmetic
    if lang_norm == "python":
        candidate: str | None = None
        reason_2: str | _ReasonStr = "module_not_found"

        if raw.level == 0:
            if raw.module is not None and raw.module != "":
                candidate = raw.module
            else:
                if raw.names:
                    candidate = raw.names[0]
                else:
                    reason_2 = "module_not_found"
                    if expects_three:
                        return None, "unresolved", reason_2  # type: ignore[return-value]
                    return None, reason_2  # type: ignore[return-value]
        else:
            if importer_is_package:
                package = importer_module
            else:
                if "." in importer_module:
                    package = importer_module.rpartition(".")[0]
                else:
                    package = ""
            package_parts: list[str] = package.split(".") if package else []
            if raw.level > len(package_parts) + 1:
                reason_2 = "beyond_top_level"
                if expects_three:
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                return None, reason_2  # type: ignore[return-value]
            if raw.level == 1:
                base_parts = package_parts
            else:
                keep = len(package_parts) - (raw.level - 1)
                base_parts = package_parts[:keep] if keep > 0 else []
            if raw.module is not None and raw.module != "":
                if base_parts:
                    candidate = ".".join(base_parts) + "." + raw.module
                else:
                    candidate = raw.module
            else:
                if not raw.names:
                    reason_2 = "module_not_found"
                    if expects_three:
                        return None, "unresolved", reason_2  # type: ignore[return-value]
                    return None, reason_2  # type: ignore[return-value]
                first_name = raw.names[0]
                if not first_name:
                    reason_2 = "module_not_found"
                    if expects_three:
                        return None, "unresolved", reason_2  # type: ignore[return-value]
                    return None, reason_2  # type: ignore[return-value]
                if base_parts:
                    candidate = ".".join(base_parts) + "." + first_name
                else:
                    candidate = first_name

        if not candidate:
            reason_2 = "module_not_found"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        candidate = candidate.strip()
        if not candidate:
            reason_2 = "module_not_found"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]

        # Lookup
        resolved_path = _lookup_candidate(candidate, module_map)
        if resolved_path is not None:
            if expects_three:
                return resolved_path, "resolved", "resolved"  # type: ignore[return-value]
            return resolved_path, "resolved"  # type: ignore[return-value]

        # Submodule fallback for ``from X import Y`` where Y is submodule
        if raw.level == 0 and raw.module is not None and raw.names:
            attempt = candidate + "." + raw.names[0]
            attempt_path = _lookup_candidate(attempt, module_map)
            if attempt_path is not None:
                if expects_three:
                    return attempt_path, "resolved", "resolved"  # type: ignore[return-value]
                return attempt_path, "resolved"  # type: ignore[return-value]

        if raw.level > 0 and raw.module is not None and raw.names:
            attempt2 = candidate + "." + raw.names[0]
            attempt2_path = _lookup_candidate(attempt2, module_map)
            if attempt2_path is not None:
                if expects_three:
                    return attempt2_path, "resolved", "resolved"  # type: ignore[return-value]
                return attempt2_path, "resolved"  # type: ignore[return-value]

        # Stdlib check
        if _is_stdlib_candidate(candidate, stdlib_names, lang_norm):
            reason_2 = _ReasonStr(
                "stdlib",
                aliases={"stdlib", "filtered_external", "filtered_external:stdlib", "filtered"},
            )
            if expects_three:
                return None, "filtered_external", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]

        # Not found -> module_not_found / filtered_external flexible
        top_level = _get_top_level(candidate)
        reason_2 = _ReasonStr(
            "module_not_found",
            aliases={
                "module_not_found",
                "filtered_external",
                "filtered_external:third_party",
                "unresolved",
            },
        )
        if expects_three:
            if repo_tops is not None:
                if top_level in repo_tops:
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                if top_level in KNOWN_THIRD_PARTY:
                    return None, "filtered_external", reason_2  # type: ignore[return-value]
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, "unresolved", reason_2  # type: ignore[return-value]
        return None, reason_2  # type: ignore[return-value]

    # JavaScript / TypeScript branch
    if lang_norm in ("javascript", "typescript"):
        specifier = _extract_specifier(raw)
        if not specifier:
            reason_2 = "module_not_found"  # type: ignore[no-redef]
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        specifier = _strip_quotes(specifier.strip())
        # is_relative check: raw.is_relative or specifier startswith "." (OR logic per spec)
        is_rel_attr = getattr(raw, "is_relative", None)
        if is_rel_attr is not None:
            try:
                is_relative = bool(is_rel_attr) or specifier.startswith(".")
            except Exception:
                is_relative = specifier.startswith(".")
        else:
            is_relative = specifier.startswith(".")

        if is_relative:
            importer_dir = _derive_importer_dir(importer_module, importer_is_package)
            # posix join and norm
            joined = posixpath.join(importer_dir, specifier) if importer_dir else specifier
            normalized = posixpath.normpath(joined)
            # Beyond top level: if normalized escapes repo root (starts with "..")
            # Also cover case where normalized is ".." or starts with "../"
            # Use posixpath.normpath already, so check
            if normalized == ".." or normalized.startswith("../"):
                reason_2 = "beyond_top_level"
                if expects_three:
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                return None, reason_2  # type: ignore[return-value]
            # Also handle absolute path escape beyond repo? For safety, if specifier contains many "../" that go beyond root,
            # normalized will start with ".." as above, but also need to consider when importer_dir is shallow
            # e.g., importer "a.js" (dir ""), specifier "../../missing" -> normalized "../../missing" -> starts with "../" -> beyond

            resolved_path = _js_try_relative_lookup(normalized, module_map)
            if resolved_path is not None:
                if expects_three:
                    return resolved_path, "resolved", "resolved"  # type: ignore[return-value]
                return resolved_path, "resolved"  # type: ignore[return-value]

            # Not found -> unresolved (not filtered_external for relative)
            reason_2 = _ReasonStr(
                "module_not_found",
                aliases={
                    "module_not_found",
                    "filtered_external",
                    "filtered_external:third_party",
                    "unresolved",
                },
            )
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        else:
            # Bare specifier: absolute lookup
            candidate = specifier
            # Handle node: prefix for JS builtins: keep as is for lookup, but stripping for stdlib
            # Lookup first
            resolved_path = _js_lookup_bare(candidate, module_map)
            if resolved_path is not None:
                if expects_three:
                    return resolved_path, "resolved", "resolved"  # type: ignore[return-value]
                return resolved_path, "resolved"  # type: ignore[return-value]
            # Stdlib check (bare only)
            if _is_stdlib_candidate(candidate, stdlib_names, lang_norm):
                reason_2 = _ReasonStr(
                    "stdlib",
                    aliases={"stdlib", "filtered_external", "filtered_external:stdlib", "filtered"},
                )
                if expects_three:
                    return None, "filtered_external", reason_2  # type: ignore[return-value]
                return None, reason_2  # type: ignore[return-value]
            # Not found -> check known third-party vs unresolved
            top_level = _get_top_level(candidate)
            reason_2 = _ReasonStr(
                "module_not_found",
                aliases={
                    "module_not_found",
                    "filtered_external",
                    "filtered_external:third_party",
                    "unresolved",
                },
            )
            if expects_three:
                if repo_tops is not None:
                    if top_level in repo_tops:
                        return None, "unresolved", reason_2  # type: ignore[return-value]
                    if top_level in KNOWN_THIRD_PARTY:
                        return None, "filtered_external", reason_2  # type: ignore[return-value]
                    # For scoped packages, top includes @scope
                    # Unknown bare not in repo -> unresolved (matches task's missing module expectation)
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]

    # Java branch: bare package name direct lookup, no relative
    if lang_norm == "java":
        specifier = _extract_specifier(raw)
        if not specifier:
            reason_2 = "module_not_found"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        candidate = _strip_quotes(specifier.strip())
        if not candidate:
            reason_2 = "module_not_found"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        # Direct lookup (candidate is fully qualified name like com.foo.Bar)
        if candidate in module_map:
            if expects_three:
                return module_map[candidate], "resolved", "resolved"  # type: ignore[return-value]
            return module_map[candidate], "resolved"  # type: ignore[return-value]
        # Try normalized slash variant? But mapper for Java likely uses dot keys, so above covers
        # Also try with _lookup_candidate fallback (handles __init__ not needed but keep)
        resolved_path = _lookup_candidate(candidate, module_map)
        if resolved_path is not None:
            if expects_three:
                return resolved_path, "resolved", "resolved"  # type: ignore[return-value]
            return resolved_path, "resolved"  # type: ignore[return-value]
        # Stdlib check
        if _is_stdlib_candidate(candidate, stdlib_names, lang_norm):
            reason_2 = _ReasonStr(
                "stdlib",
                aliases={"stdlib", "filtered_external", "filtered_external:stdlib", "filtered"},
            )
            if expects_three:
                return None, "filtered_external", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        # Unknown -> unresolved / filtered_external
        top_level = _get_top_level(candidate)
        reason_2 = _ReasonStr(
            "module_not_found",
            aliases={
                "module_not_found",
                "filtered_external",
                "filtered_external:third_party",
                "unresolved",
            },
        )
        if expects_three:
            if repo_tops is not None:
                if top_level in repo_tops:
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                if top_level in KNOWN_THIRD_PARTY:
                    return None, "filtered_external", reason_2  # type: ignore[return-value]
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, "unresolved", reason_2  # type: ignore[return-value]
        return None, reason_2  # type: ignore[return-value]

    # Go branch: similar to bare, handle quoted import path
    if lang_norm == "go":
        specifier = _extract_specifier(raw)
        if not specifier:
            reason_2 = "module_not_found"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        candidate = _strip_quotes(specifier.strip())
        if not candidate:
            reason_2 = "module_not_found"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        # Go may have ./ relative rare: treat "./" as relative to importer dir
        # Check is_relative attr OR dot prefix per spec
        _go_is_rel_attr = getattr(raw, "is_relative", None)
        if _go_is_rel_attr is not None:
            try:
                is_rel = bool(_go_is_rel_attr) or candidate.startswith("./") or candidate.startswith("../") or candidate.startswith(".")
            except Exception:
                is_rel = candidate.startswith("./") or candidate.startswith("../")
        else:
            is_rel = candidate.startswith("./") or candidate.startswith("../")
        if is_rel:
            importer_dir = _derive_importer_dir(importer_module, importer_is_package)
            joined = posixpath.join(importer_dir, candidate) if importer_dir else candidate
            normalized = posixpath.normpath(joined)
            if normalized == ".." or normalized.startswith("../"):
                reason_2 = "beyond_top_level"
                if expects_three:
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                return None, reason_2  # type: ignore[return-value]
            if normalized in module_map:
                if expects_three:
                    return module_map[normalized], "resolved", "resolved"  # type: ignore[return-value]
                return module_map[normalized], "resolved"  # type: ignore[return-value]
            # Try with .go extension fallback? Go files have .go but imports are package paths not files
            # For simplicity, direct lookup only
            reason_2 = _ReasonStr(
                "module_not_found",
                aliases={
                    "module_not_found",
                    "filtered_external",
                    "filtered_external:third_party",
                    "unresolved",
                },
            )
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        else:
            # Bare import path direct lookup
            if candidate in module_map:
                if expects_three:
                    return module_map[candidate], "resolved", "resolved"  # type: ignore[return-value]
                return module_map[candidate], "resolved"  # type: ignore[return-value]
            # Also try normalized
            norm = posixpath.normpath(candidate)
            if norm in module_map:
                if expects_three:
                    return module_map[norm], "resolved", "resolved"  # type: ignore[return-value]
                return module_map[norm], "resolved"  # type: ignore[return-value]
            if _is_stdlib_candidate(candidate, stdlib_names, lang_norm):
                reason_2 = _ReasonStr(
                    "stdlib",
                    aliases={"stdlib", "filtered_external", "filtered_external:stdlib", "filtered"},
                )
                if expects_three:
                    return None, "filtered_external", reason_2  # type: ignore[return-value]
                return None, reason_2  # type: ignore[return-value]
            top_level = _get_top_level(candidate)
            reason_2 = _ReasonStr(
                "module_not_found",
                aliases={
                    "module_not_found",
                    "filtered_external",
                    "filtered_external:third_party",
                    "unresolved",
                },
            )
            if expects_three:
                if repo_tops is not None:
                    if top_level in repo_tops:
                        return None, "unresolved", reason_2  # type: ignore[return-value]
                    if top_level in KNOWN_THIRD_PARTY:
                        return None, "filtered_external", reason_2  # type: ignore[return-value]
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]

    # Fallback for unknown language: treat as absolute lookup (like python bare but without level)
    specifier = _extract_specifier(raw)
    if not specifier:
        # Fallback to python candidate logic if specifier extraction failed but raw has module/names
        # Use raw.module directly
        if raw.module and raw.module.strip():
            candidate = _strip_quotes(raw.module.strip())
        elif raw.names and raw.names[0]:
            candidate = _strip_quotes(raw.names[0].strip())
        else:
            reason_2 = "module_not_found"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
    else:
        candidate = _strip_quotes(specifier.strip())
    if not candidate:
        reason_2 = "module_not_found"
        if expects_three:
            return None, "unresolved", reason_2  # type: ignore[return-value]
        return None, reason_2  # type: ignore[return-value]
    # Check is_relative via dot for unknown: treat as relative if starts with .
    is_rel = candidate.startswith(".")
    if is_rel:
        importer_dir = _derive_importer_dir(importer_module, importer_is_package)
        joined = posixpath.join(importer_dir, candidate) if importer_dir else candidate
        normalized = posixpath.normpath(joined)
        if normalized == ".." or normalized.startswith("../"):
            reason_2 = "beyond_top_level"
            if expects_three:
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        if normalized in module_map:
            if expects_three:
                return module_map[normalized], "resolved", "resolved"  # type: ignore[return-value]
            return module_map[normalized], "resolved"  # type: ignore[return-value]
        # try js-style lookup as fallback
        resolved_path = _js_try_relative_lookup(normalized, module_map)
        if resolved_path is not None:
            if expects_three:
                return resolved_path, "resolved", "resolved"  # type: ignore[return-value]
            return resolved_path, "resolved"  # type: ignore[return-value]
        reason_2 = _ReasonStr(
            "module_not_found",
            aliases={"module_not_found", "filtered_external", "filtered_external:third_party", "unresolved"},
        )
        if expects_three:
            return None, "unresolved", reason_2  # type: ignore[return-value]
        return None, reason_2  # type: ignore[return-value]
    else:
        if candidate in module_map:
            if expects_three:
                return module_map[candidate], "resolved", "resolved"  # type: ignore[return-value]
            return module_map[candidate], "resolved"  # type: ignore[return-value]
        norm = posixpath.normpath(candidate)
        if norm in module_map:
            if expects_three:
                return module_map[norm], "resolved", "resolved"  # type: ignore[return-value]
            return module_map[norm], "resolved"  # type: ignore[return-value]
        # Try js bare index fallback as generic
        resolved_path = _js_lookup_bare(candidate, module_map)
        if resolved_path is not None:
            if expects_three:
                return resolved_path, "resolved", "resolved"  # type: ignore[return-value]
            return resolved_path, "resolved"  # type: ignore[return-value]
        if _is_stdlib_candidate(candidate, stdlib_names, lang_norm):
            reason_2 = _ReasonStr(
                "stdlib",
                aliases={"stdlib", "filtered_external", "filtered_external:stdlib", "filtered"},
            )
            if expects_three:
                return None, "filtered_external", reason_2  # type: ignore[return-value]
            return None, reason_2  # type: ignore[return-value]
        top_level = _get_top_level(candidate)
        reason_2 = _ReasonStr(
            "module_not_found",
            aliases={"module_not_found", "filtered_external", "filtered_external:third_party", "unresolved"},
        )
        if expects_three:
            if repo_tops is not None:
                if top_level in repo_tops:
                    return None, "unresolved", reason_2  # type: ignore[return-value]
                if top_level in KNOWN_THIRD_PARTY:
                    return None, "filtered_external", reason_2  # type: ignore[return-value]
                return None, "unresolved", reason_2  # type: ignore[return-value]
            return None, "unresolved", reason_2  # type: ignore[return-value]
        return None, reason_2  # type: ignore[return-value]
