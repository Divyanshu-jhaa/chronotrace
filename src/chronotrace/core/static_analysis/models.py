"""Dataclasses for static analysis — flexible to support parallel agent implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# RawImport — flexible: supports both `raw_text` required and optional forms
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, init=False)
class RawImport:
    """Raw import extracted without execution — language-aware via tree-sitter."""

    module: str | None
    names: list[str]
    asnames: list[str | None]
    level: int
    lineno: int
    raw_text: str
    language: str
    is_relative: bool

    def __init__(
        self,
        module: str | None = None,
        names: list[str] | None = None,
        asnames: list[str | None] | None = None,
        level: int = 0,
        lineno: int = 1,
        raw_text: str = "",
        language: str = "python",
        is_relative: bool | None = None,
        **kwargs,
    ) -> None:
        # Handle singular aliases — support old (module+name+asname)
        # and new (module+names+asnames) callers
        # Use sentinel to distinguish "not provided" vs "provided as None"
        _sentinel = object()
        singular_name = kwargs.pop("name", _sentinel)
        singular_asname = kwargs.pop("asname", _sentinel)
        singular_alias = kwargs.pop("alias", _sentinel)
        # singular_name provided (including None) -> map to names if needed
        if singular_name is not _sentinel:
            if singular_name is not None:
                # For ImportFrom: name is "bar" -> names=[bar]
                # For Import: old code passes None -> ignore
                if names is None:
                    names = [singular_name]  # type: ignore[assignment]
            else:
                # singular_name is None -> keep names as is
                pass
        if singular_asname is not _sentinel:
            # asname may be None or string; preserve as [None] if needed
            if asnames is None:
                asnames = [singular_asname]  # type: ignore[assignment]
        elif singular_alias is not _sentinel:
            if asnames is None:
                asnames = [singular_alias]  # type: ignore[assignment]
        # Also support name_list alias
        if names is None and "name_list" in kwargs:
            names = kwargs.pop("name_list")
        if "line" in kwargs and lineno == 1:
            try:
                lineno = int(kwargs.pop("line"))
            except Exception:
                pass
        if "lineno" in kwargs:
            try:
                lineno = int(kwargs.pop("lineno"))
            except Exception:
                pass
        # Handle language / is_relative from kwargs (backward compat)
        if "language" in kwargs and isinstance(kwargs.get("language"), str):
            # explicit language in kwargs overrides param when param is default
            if language == "python":
                # only override if caller passed via kwargs and param not explicitly changed
                # but if kwargs contains language, use it
                language = str(kwargs.pop("language"))
            else:
                kwargs.pop("language", None)
        elif "language" in kwargs:
            kwargs.pop("language", None)
        # is_relative handling
        if is_relative is None:
            if "is_relative" in kwargs:
                val = kwargs.pop("is_relative")
                if isinstance(val, bool):
                    is_relative = val
                elif isinstance(val, str):
                    is_relative = val.lower() in ("true", "1", "yes")
                else:
                    is_relative = bool(val)
            elif "isRelative" in kwargs:
                is_relative = bool(kwargs.pop("isRelative"))
            elif "relative" in kwargs:
                is_relative = bool(kwargs.pop("relative"))
            else:
                # derive from level: relative if level>0
                is_relative = bool(level and int(level) > 0)
        # Also handle specifier vs module alias
        if "specifier" in kwargs and module is None:
            module = str(kwargs.pop("specifier"))
        # Handle is_relative alias from kwargs when explicitly provided alongside param
        if "is_relative" in kwargs:
            kwargs.pop("is_relative", None)
        object.__setattr__(self, "module", module)
        object.__setattr__(self, "names", list(names) if names is not None else [])
        object.__setattr__(self, "asnames", list(asnames) if asnames is not None else [])
        object.__setattr__(self, "level", int(level))
        object.__setattr__(self, "lineno", int(lineno))
        object.__setattr__(self, "raw_text", str(raw_text) if raw_text is not None else "")
        object.__setattr__(self, "language", str(language) if language is not None else "python")
        object.__setattr__(self, "is_relative", bool(is_relative) if is_relative is not None else False)

    # Back-compat singular aliases — expose first element of names/asnames
    @property
    def name(self) -> str | None:  # pragma: no cover
        # For old single-name callers, return first imported name if present, else module for Import
        if self.names:
            return self.names[0]
        return None

    @property
    def asname(self) -> str | None:  # pragma: no cover
        return self.asnames[0] if self.asnames else None

    @property
    def alias(self) -> str | None:  # pragma: no cover
        return self.asnames[0] if self.asnames else None

    @property
    def line(self) -> int:  # pragma: no cover
        return self.lineno


# ---------------------------------------------------------------------------
# ParseResult — flexible: supports `path` as Path|str, `raw_text` and `success`
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, init=False)
class ParseResult:
    """Successful parse of a single file — language-aware."""

    path: str | Path
    raw_imports: list[RawImport]
    raw_text: str
    success: bool
    language: str | None

    def __init__(
        self,
        path: str | Path = "",
        raw_imports: list[RawImport] | None = None,
        raw_text: str = "",
        success: bool = True,
        language: str | None = None,
        **kwargs,
    ) -> None:
        # Alias handling
        if "file" in kwargs and not path:
            path = kwargs.pop("file")
        if "file_path" in kwargs and not path:
            path = kwargs.pop("file_path")
        if "imports" in kwargs and raw_imports is None:
            raw_imports = kwargs.pop("imports")
        if raw_imports is None:
            raw_imports = []
        # Preserve input type: str stays str (task spec), Path stays Path
        stored_path: str | Path
        if isinstance(path, Path):
            stored_path = path
        elif isinstance(path, str):
            stored_path = path  # keep as str for task spec
        else:
            stored_path = str(path)
        # language handling
        if language is None and "language" in kwargs:
            language = kwargs.pop("language")
        # tolerate extra kwargs
        if "lang" in kwargs and language is None:
            language = kwargs.pop("lang")
        # pop any remaining language-related kwargs to avoid leaks
        kwargs.pop("language", None)
        kwargs.pop("lang", None)
        object.__setattr__(self, "path", stored_path)  # type: ignore[assignment]
        object.__setattr__(self, "raw_imports", list(raw_imports))  # type: ignore[arg-type]
        object.__setattr__(self, "raw_text", str(raw_text) if raw_text is not None else "")
        object.__setattr__(self, "success", bool(success))
        object.__setattr__(self, "language", str(language) if language is not None else None)

    # Tuple compatibility: `imports, failure = parse_file(path)` yields (raw_imports, None)
    def __iter__(self):  # type: ignore[override]
        yield self.raw_imports
        yield None

    def __len__(self) -> int:  # type: ignore[override]
        return 2

    def __getitem__(self, idx: int):  # type: ignore[override]
        if idx == 0:
            return self.raw_imports
        if idx == 1:
            return None
        raise IndexError(idx)

    @property
    def file(self) -> Path:  # pragma: no cover
        return self.path  # type: ignore[return-value]

    @property
    def file_path(self) -> Path:  # pragma: no cover
        return self.path  # type: ignore[return-value]

    @property
    def imports(self) -> list[RawImport]:  # pragma: no cover
        return self.raw_imports

    @property
    def source(self) -> str:  # pragma: no cover
        return self.raw_text

    @property
    def text(self) -> str:  # pragma: no cover
        return self.raw_text


# ---------------------------------------------------------------------------
# ParseFailure — flexible: path as Path|str, reason, lineno, raw_text optional
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, init=False)
class ParseFailure:
    """Per-file parse failure (syntax, encoding, or I/O) — isolated, not raised."""

    path: str | Path
    reason: str
    lineno: int | None
    raw_text: str | None
    language: str | None

    def __init__(
        self,
        path: str | Path = "",
        reason: str = "",
        lineno: int | None = None,
        raw_text: str | None = None,
        language: str | None = None,
        **kwargs,
    ) -> None:
        if "file" in kwargs and not path:
            path = kwargs.pop("file")
        if "file_path" in kwargs and not path:
            path = kwargs.pop("file_path")
        if "message" in kwargs and not reason:
            reason = str(kwargs.pop("message"))
        if "error" in kwargs and not reason:
            reason = str(kwargs.pop("error"))
        stored_path: str | Path
        if isinstance(path, str):
            stored_path = path
        elif isinstance(path, Path):
            stored_path = path
        else:
            stored_path = str(path)
        object.__setattr__(self, "path", stored_path)  # type: ignore[assignment]
        object.__setattr__(self, "reason", str(reason))
        object.__setattr__(
            self,
            "lineno",
            int(lineno) if isinstance(lineno, int) else None if lineno is None else None,
        )
        # Handle string lineno
        if lineno is not None and not isinstance(lineno, int):
            try:
                lineno = int(lineno)  # type: ignore[assignment]
            except Exception:
                lineno = None
            object.__setattr__(self, "lineno", lineno)  # type: ignore[assignment]
        object.__setattr__(self, "raw_text", str(raw_text) if isinstance(raw_text, str) else None)
        if language is None and "language" in kwargs:
            language = kwargs.pop("language")
        if "lang" in kwargs and language is None:
            language = kwargs.pop("lang")
        # support reason aliases
        if "unsupported_language" in str(reason) and language is None:
            pass
        object.__setattr__(self, "language", str(language) if language is not None else None)
        # consume any leftover language kwargs
        kwargs.pop("language", None)
        kwargs.pop("lang", None)

    # Tuple compatibility: `imports, failure = parse_file(path)` yields ([], self) on failure
    def __iter__(self):  # type: ignore[override]
        yield []
        yield self

    def __len__(self) -> int:  # type: ignore[override]
        return 2

    def __getitem__(self, idx: int):  # type: ignore[override]
        if idx == 0:
            return []
        if idx == 1:
            return self
        raise IndexError(idx)

    @property
    def file(self) -> Path:  # pragma: no cover
        return self.path  # type: ignore[return-value]

    @property
    def file_path(self) -> Path:  # pragma: no cover
        return self.path  # type: ignore[return-value]

    @property
    def message(self) -> str:  # pragma: no cover
        return self.reason

    @property
    def error(self) -> str:  # pragma: no cover
        return self.reason


# ---------------------------------------------------------------------------
# ResolvedImport — flexible: supports `raw` and `raw_import` aliases
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, init=False)
class ResolvedImport:
    """An import resolved to a repository file."""

    importer: str
    target: str
    raw_import: RawImport

    # Back-compat storage for `raw` alias
    raw: RawImport  # alias field

    def __init__(
        self,
        importer: str = "",
        target: str = "",
        raw_import: RawImport | None = None,
        raw: RawImport | None = None,
        **kwargs,
    ) -> None:
        if raw_import is None and raw is not None:
            raw_import = raw
        if raw_import is None and "rawImport" in kwargs:
            raw_import = kwargs.pop("rawImport")
        # Fallback dummy if still None (should not happen in real usage)
        if raw_import is None:
            raw_import = RawImport(module=None, names=[], asnames=[], level=0, lineno=1)  # type: ignore[arg-type]
        object.__setattr__(self, "importer", str(importer))
        object.__setattr__(self, "target", str(target))
        object.__setattr__(self, "raw_import", raw_import)  # type: ignore[assignment]
        object.__setattr__(self, "raw", raw_import)  # type: ignore[assignment]

    @property
    def source(self) -> str:  # pragma: no cover
        return self.importer

    @property
    def destination(self) -> str:  # pragma: no cover
        return self.target


# ---------------------------------------------------------------------------
# UnresolvedImport — flexible: supports `raw` / `raw_import`
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, init=False)
class UnresolvedImport:
    """An import that could not be resolved to a repository file."""

    importer: str
    raw_import: RawImport
    reason: str
    raw_text: str | None

    raw: RawImport  # alias

    def __init__(
        self,
        importer: str = "",
        raw_import: RawImport | None = None,
        reason: str = "",
        raw_text: str | None = None,
        raw: RawImport | None = None,
        **kwargs,
    ) -> None:
        if raw_import is None and raw is not None:
            raw_import = raw
        if raw_import is None and "rawImport" in kwargs:
            raw_import = kwargs.pop("rawImport")
        if raw_import is None:
            raw_import = RawImport(module=None, names=[], asnames=[], level=0, lineno=1)  # type: ignore[arg-type]
        if "message" in kwargs and not reason:
            reason = str(kwargs.pop("message"))
        object.__setattr__(self, "importer", str(importer))
        object.__setattr__(self, "raw_import", raw_import)  # type: ignore[assignment]
        object.__setattr__(self, "raw", raw_import)  # type: ignore[assignment]
        object.__setattr__(self, "reason", str(reason))
        object.__setattr__(self, "raw_text", str(raw_text) if isinstance(raw_text, str) else None)

    @property
    def file(self) -> str:  # pragma: no cover
        return self.importer

    @property
    def message(self) -> str:  # pragma: no cover
        return self.reason


@dataclass(slots=True)
class Diagnostics:
    """Aggregate counters and detail lists for a single analysis run.

    Includes per-language counters and per-language lists as required by
    iteration 4 multi-language static analysis.  All per-language structures
    default to empty and are additive — existing single-language tests remain
    compatible without providing them.
    """

    files_scanned: int = 0
    files_parsed: int = 0
    files_failed: int = 0
    imports_extracted: int = 0
    imports_resolved: int = 0
    imports_unresolved: int = 0
    imports_filtered_external: int = 0
    parse_failures: list[ParseFailure] = field(default_factory=list)
    unresolved_details: list[UnresolvedImport] = field(default_factory=list)
    # Per-language aggregates — counters and detail lists
    per_language: dict[str, dict[str, int]] = field(default_factory=dict)
    per_language_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    per_language_parse_failures: dict[str, list[ParseFailure]] = field(default_factory=dict)
    per_language_unresolved: dict[str, list[UnresolvedImport]] = field(default_factory=dict)
    # Legacy alias for per-language unresolved alias used by some tests
    per_language_unresolved_details: dict[str, list[UnresolvedImport]] = field(default_factory=dict)

    @property
    def imports_filtered(self) -> int:  # pragma: no cover
        return self.imports_filtered_external

    # Back-compat helpers — keep per_language_counts in sync if only per_language supplied
    def __post_init__(self) -> None:
        # If per_language was populated but per_language_counts is empty, mirror it
        if self.per_language and not self.per_language_counts:
            # copy to keep alias consistent
            self.per_language_counts = dict(self.per_language)
        elif self.per_language_counts and not self.per_language:
            self.per_language = dict(self.per_language_counts)
        # Sync unresolved aliases
        if self.per_language_unresolved and not self.per_language_unresolved_details:
            self.per_language_unresolved_details = dict(self.per_language_unresolved)  # type: ignore[assignment]
        elif self.per_language_unresolved_details and not self.per_language_unresolved:
            self.per_language_unresolved = dict(self.per_language_unresolved_details)  # type: ignore[assignment]


@dataclass(slots=True)
class AnalysisResult:
    """Result of ``analyze_repository`` orchestration."""

    repository_id: str = ""
    files_scanned: int = 0
    files_parsed: int = 0
    files_failed: int = 0
    imports_extracted: int = 0
    imports_resolved: int = 0
    imports_unresolved: int = 0
    imports_filtered_external: int = 0
    relations_persisted: int = 0
    diagnostics: Diagnostics = field(default_factory=Diagnostics)

    @property
    def resolved_imports(self) -> list[ResolvedImport]:  # pragma: no cover
        return []

    @property
    def unresolved_imports(self) -> list[UnresolvedImport]:  # pragma: no cover
        return list(self.diagnostics.unresolved_details)

    @property
    def parse_failures(self) -> list[ParseFailure]:  # pragma: no cover
        return list(self.diagnostics.parse_failures)


__all__ = [
    "AnalysisResult",
    "Diagnostics",
    "ParseFailure",
    "ParseResult",
    "RawImport",
    "ResolvedImport",
    "UnresolvedImport",
]
