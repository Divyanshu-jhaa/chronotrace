# Spec Delta

## Purpose

Provides the file-level structural dependency layer for ChronoTrace by discovering Python sources, extracting imports via AST without execution, resolving intra-repository imports, and persisting the current import graph for downstream analytics.

## ADDED Requirements

### Requirement: Python source discovery

The system SHALL discover Python source files under a repository root by recursively scanning for `*.py` files, including package `__init__.py` files, normalizing paths to canonical POSIX-relative form and applying configured exclusion patterns.

#### Scenario: Discover files in a flat package

- **WHEN** a repository contains `foo.py`, `bar.py`, and `pkg/__init__.py` with `pkg/mod.py`
- **THEN** discovery returns four paths normalized as `foo.py`, `bar.py`, `pkg/__init__.py`, `pkg/mod.py`

#### Scenario: Exclusion patterns filter files

- **WHEN** exclusion patterns include `tests/**` and `.*`
- **THEN** files under `tests/` and dot-prefixed paths are excluded from analysis

#### Scenario: Path normalization consistency

- **WHEN** discovery runs on Windows and POSIX
- **THEN** all returned paths use the same forward-slash canonical form as ingestion (relative to repo root)

### Requirement: AST parsing without execution

The system SHALL parse each discovered Python file using the Python `ast` module without executing its code and extract `import` and `from ... import` statements with module name, imported names, alias, level, and source line.

#### Scenario: Extract absolute imports

- **WHEN** a file contains `import os` and `import foo.bar as fb` and `from foo import baz`
- **THEN** the parser returns three raw import records with correct module names (`os`, `foo.bar`, `foo`), imported names, alias (`fb`), level `0`, and line numbers

#### Scenario: Extract relative imports

- **WHEN** a file `pkg/mod.py` contains `from . import sibling` and `from ..utils import helper`
- **THEN** the parser returns records with levels `1` and `2` respectively, with module values (`None` for the first, `utils` for the second) and correct line numbers

#### Scenario: No code execution

- **WHEN** a file contains top-level side effects (e  g , `open('/tmp/x','w')` or `print('hi')` at module scope)
- **THEN** parsing does not execute those effects and still extracts the import statements

### Requirement: Module-to-file mapping with package handling

The system SHALL build a repository-scoped map from Python module names to canonical file paths, correctly handling top-level modules, nested packages, and package `__init__.py` files.

#### Scenario: Map flat and package modules

- **WHEN** files `app.py`, `pkg/__init__.py`, `pkg/sub.py`, and `pkg/sub/nested.py` exist
- **THEN** the map contains `app -> app.py`, `pkg -> pkg/__init__.py`, `pkg.sub -> pkg/sub.py`, `pkg.sub.nested -> pkg/sub/nested.py`

#### Scenario: Package import resolves to __init__

- **WHEN** another file imports `pkg`
- **THEN** resolution returns `pkg/__init__.py` as the target

### Requirement: Absolute import resolution

The system SHALL resolve absolute imports to repository files via the module-to-file map, accounting for package `__init__.py` targets.

#### Scenario: Resolve sibling module

- **WHEN** `a.py` contains `import b` and `b.py` exists
- **THEN** the import resolves to `b.py`

#### Scenario: Resolve dotted absolute import

- **WHEN** `main.py` contains `from pkg.sub import foo` and `pkg/sub.py` exists
- **THEN** resolution identifies the target as `pkg/sub.py` (or `pkg/sub/__init__.py` if package)

### Requirement: Relative import resolution

The system SHALL resolve relative imports using the importing module's package context, supporting single- and multi-level relative imports and preserving `__init__.py` package semantics.

#### Scenario: Single-level relative within package

- **WHEN** `pkg/a.py` (module `pkg.a`) contains `from . import b` and `pkg/b.py` exists
- **THEN** the import resolves to `pkg/b.py`

#### Scenario: Parent-level relative

- **WHEN** `pkg/sub/mod.py` (module `pkg.sub.mod`) contains `from .. import utils` and `pkg/utils.py` exists
- **THEN** the import resolves to `pkg/utils.py`

#### Scenario: Import inside __init__

- **WHEN** `pkg/__init__.py` (module `pkg`) contains `from .sub import x` and `pkg/sub.py` exists
- **THEN** resolution uses package `pkg` as base and resolves correctly

#### Scenario: Beyond-top-level relative is unresolved

- **WHEN** `a.py` (module `a`, top-level) contains `from .. import x`
- **THEN** the import is classified as unresolved with reason `beyond_top_level`

### Requirement: Stdlib and third-party filtering

The system SHALL exclude standard-library and third-party imports so that only imports resolved to files within the repository produce graph edges, and SHALL NOT create `File` nodes for external modules.

#### Scenario: Stdlib imports are filtered

- **WHEN** a file imports `os`, `sys`, `json`, or `pathlib`
- **THEN** those imports are classified as filtered external and produce no graph edge

#### Scenario: Third-party imports are filtered

- **WHEN** a file imports `requests`, `neo4j`, or `typer` which are not present in the module map
- **THEN** those imports are classified as filtered external and produce no graph edge

#### Scenario: No false nodes for external modules

- **WHEN** analysis completes
- **THEN** the graph contains no `File` nodes whose path corresponds to a stdlib or third-party module name

### Requirement: Import graph persistence with rebuild semantics

The system SHALL persist only resolved intra-repository imports as `(:File)-[:IMPORTS]->(:File)` relationships scoped to the repository, and re-running analysis on the same snapshot SHALL replace stale edges without duplicating them.

#### Scenario: Persist resolved edges

- **WHEN** `a.py` imports `b.py` via a resolved import
- **THEN** a single `IMPORTS` relationship from the `File` node for `a.py` to the `File` node for `b.py` exists in Neo4j

#### Scenario: Re-run is idempotent

- **WHEN** static analysis is executed twice consecutively on an unchanged snapshot
- **THEN** the graph still contains exactly one `IMPORTS` edge per resolved import pair

#### Scenario: Stale edges are removed on rebuild

- **WHEN** a previously imported file `b.py` is no longer imported after an edit, and analysis is re-run
- **THEN** the former `IMPORTS` edge `a.py -> b.py` is removed

#### Scenario: Repository isolation

- **WHEN** two repositories each contain a file `main.py` importing `lib.py`
- **THEN** each repository's import edges are scoped to its own `File` nodes and do not cross

### Requirement: Parse-failure isolation and diagnostics

The system SHALL isolate per-file parse failures so that one file's syntax error, encoding issue, or I/O error does not abort analysis of remaining files, and SHALL record failures for diagnostics.

#### Scenario: Syntax error does not abort run

- **WHEN** one file contains a `SyntaxError` and other files are valid
- **THEN** analysis completes for the valid files, the failing file is counted in `files_failed`, and a `ParseFailure` with path and reason is recorded

#### Scenario: Encoding or I/O error captured

- **WHEN** a file cannot be read due to encoding or permission issues
- **THEN** the failure is recorded with reason and the file is counted as failed without raising to the caller

### Requirement: Unresolved import diagnostics and counters

The system SHALL produce analysis diagnostics including counts of files scanned/parsed/failed, imports extracted/resolved/unresolved/filtered, and lists of parse failures and unresolved import details.

#### Scenario: Diagnostics counters accurate

- **WHEN** a repository has 5 files, 2 fail to parse, 10 imports extracted, 6 resolve intra-repo, 2 are stdlib, 2 are unresolved
- **THEN** diagnostics report `files_scanned=5`, `files_parsed=3`, `files_failed=2`, `imports_extracted=10`, `imports_resolved=6`, `imports_filtered_external=2`, `imports_unresolved=2`

#### Scenario: Unresolved reason surfaced

- **WHEN** an import cannot be resolved (missing module, beyond-top-level, namespace package)
- **THEN** the unresolved entry includes importer path, raw import text, and reason

### Requirement: Documented limitations

The system SHALL document that dynamic imports, namespace packages without `__init__.py`, generated modules not present on disk, and absence of a static edge not proving absence of a runtime dependency are limitations of static analysis.

#### Scenario: Limitations documented

- **WHEN** a consumer reads the analysis docs or API response
- **THEN** the limitations above are stated, and the system does not claim completeness for dynamic imports

