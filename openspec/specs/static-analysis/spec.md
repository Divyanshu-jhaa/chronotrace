# static-analysis Specification

## Purpose

Provides the file-level structural dependency layer for ChronoTrace by discovering sources for all supported languages, extracting imports via tree-sitter without execution, resolving intra-repository imports per language, and persisting the current import graph for downstream analytics.

## Requirements

### Requirement: Multi-language source discovery

The system SHALL discover source files under a repository root by recursively scanning for configured extensions (e.g., `*.py`, `*.js`, `*.jsx`, `*.ts`, `*.tsx`, `*.java`, `*.go`, `*.rs`, etc.), including package entry files, normalizing paths to canonical POSIX-relative form and applying configured exclusion patterns.

#### Scenario: Discover files across languages

- **WHEN** a repository contains `foo.py`, `pkg/__init__.py` with `pkg/mod.py`, `src/index.js`, `src/utils.ts`, and `com/foo/Bar.java`
- **THEN** discovery returns paths normalized as `foo.py`, `pkg/__init__.py`, `pkg/mod.py`, `src/index.js`, `src/utils.ts`, `com/foo/Bar.java`

#### Scenario: Exclusion patterns filter files

- **WHEN** exclusion patterns include `tests/**`, `node_modules/**`, and `.*`
- **THEN** files under `tests/` and `node_modules/` and dot-prefixed paths are excluded from analysis

#### Scenario: Path normalization consistency

- **WHEN** discovery runs on Windows and POSIX
- **THEN** all returned paths use the same forward-slash canonical form as ingestion (relative to repo root)

### Requirement: Tree-sitter parsing without execution

The system SHALL parse each discovered source file using tree-sitter (language-specific `Language` + `Parser`) without executing its code and extract import-like statements with raw specifier, imported names, alias, relative vs absolute form, and source line/column.

#### Scenario: Extract Python imports via tree-sitter

- **WHEN** a Python file contains `import os` and `import foo.bar as fb` and `from foo import baz`
- **THEN** the parser returns three raw import records with correct specifiers (`os`, `foo.bar`, `foo`), imported names, alias (`fb`), relative flag, and line numbers

#### Scenario: Extract JavaScript/TypeScript imports via tree-sitter

- **WHEN** a file `app.ts` contains `import {x} from "./utils"` and `const y = require("../lib")`
- **THEN** the parser returns records with specifiers `./utils` and `../lib`, correct imported names, and line numbers

#### Scenario: Extract Java and Go imports via tree-sitter

- **WHEN** a Java file contains `import com.foo.Bar;` and a Go file contains `import "github.com/org/lib"`
- **THEN** the parser returns records with specifiers `com.foo.Bar` and `github.com/org/lib` and line numbers

#### Scenario: No code execution

- **WHEN** a file contains top-level side effects (e.g., `open('/tmp/x','w')` or `console.log('hi')` at module scope)
- **THEN** parsing does not execute those effects and still extracts the import statements

### Requirement: Language-aware module-to-file mapping

The system SHALL build a repository-scoped map from logical module specifiers to canonical file paths per language, correctly handling entry files and nested packages.

#### Scenario: Map Python packages correctly

- **WHEN** files `app.py`, `pkg/__init__.py`, `pkg/sub.py`, and `pkg/sub/nested.py` exist
- **THEN** the map contains `app -> app.py`, `pkg -> pkg/__init__.py`, `pkg.sub -> pkg/sub.py`, `pkg.sub.nested -> pkg/sub/nested.py`

#### Scenario: Map JavaScript and Java modules

- **WHEN** files `src/index.js`, `src/utils.ts`, and `com/foo/Bar.java` exist
- **THEN** the map contains `src -> src/index.js` (or `src/index.js` as package entry), `src/utils -> src/utils.ts`, `com.foo.Bar -> com/foo/Bar.java`

#### Scenario: Package import resolves to entry file

- **WHEN** another file imports `pkg` (Python) or `src` (JS)
- **THEN** resolution returns `pkg/__init__.py` or `src/index.js` as the target

### Requirement: Language-specific absolute import resolution

The system SHALL resolve absolute/bare import specifiers to repository files via the language-aware module-to-file map, accounting for package entry targets.

#### Scenario: Resolve Python sibling module

- **WHEN** `a.py` contains `import b` and `b.py` exists
- **THEN** the import resolves to `b.py`

#### Scenario: Resolve JavaScript bare import

- **WHEN** `main.js` contains `import foo from "pkg/sub"` and `pkg/sub/index.js` exists
- **THEN** resolution identifies the target as `pkg/sub/index.js` (or `pkg/sub.js` if file)

#### Scenario: Resolve Java absolute import

- **WHEN** `Main.java` contains `import com.foo.Bar` and `com/foo/Bar.java` exists
- **THEN** resolution identifies the target as `com/foo/Bar.java`

### Requirement: Language-specific relative import resolution

The system SHALL resolve relative imports using the importing file's package context per language semantics, supporting single- and multi-level relatives and preserving entry-file handling.

#### Scenario: Single-level relative within package (Python)

- **WHEN** `pkg/a.py` (module `pkg.a`) contains `from . import b` and `pkg/b.py` exists
- **THEN** the import resolves to `pkg/b.py`

#### Scenario: Parent-level relative (Python)

- **WHEN** `pkg/sub/mod.py` (module `pkg.sub.mod`) contains `from .. import utils` and `pkg/utils.py` exists
- **THEN** the import resolves to `pkg/utils.py`

#### Scenario: Relative import inside __init__

- **WHEN** `pkg/__init__.py` (module `pkg`) contains `from .sub import x` and `pkg/sub.py` exists
- **THEN** resolution uses package `pkg` as base and resolves correctly

#### Scenario: JavaScript relative resolution

- **WHEN** `src/app.js` contains `import x from "./utils"` and `src/utils.js` exists
- **THEN** the import resolves to `src/utils.js`

#### Scenario: Beyond-top-level relative is unresolved

- **WHEN** `a.py` (module `a`, top-level) contains `from .. import x`
- **THEN** the import is classified as unresolved with reason `beyond_top_level`

### Requirement: Stdlib and external filtering

The system SHALL exclude standard-library and third-party/external imports so that only imports resolved to files within the repository produce graph edges, and SHALL NOT create `File` nodes for external modules.

#### Scenario: Stdlib imports are filtered

- **WHEN** a Python file imports `os`, `sys`, `json`, or a Java file imports `java.util.*`
- **THEN** those imports are classified as filtered external and produce no graph edge

#### Scenario: Third-party imports are filtered

- **WHEN** a file imports `requests`, `neo4j`, `react`, or `lodash` which are not present in the module map
- **THEN** those imports are classified as filtered external and produce no graph edge

#### Scenario: No false nodes for external modules

- **WHEN** analysis completes
- **THEN** the graph contains no `File` nodes whose path corresponds to a stdlib or third-party module name

### Requirement: Import graph persistence with rebuild semantics

The system SHALL persist only resolved intra-repository imports as `(:File)-[:IMPORTS]->(:File)` relationships scoped to the repository, and re-running analysis on the same snapshot SHALL replace stale edges without duplicating them.

#### Scenario: Persist resolved edges

- **WHEN** `a.py` imports `b.py` (Python) or `app.js` imports `utils.js` via a resolved import
- **THEN** a single `IMPORTS` relationship from the `File` node for the source to the `File` node for the target exists in Neo4j

#### Scenario: Re-run is idempotent

- **WHEN** static analysis is executed twice consecutively on an unchanged snapshot
- **THEN** the graph still contains exactly one `IMPORTS` edge per resolved import pair

#### Scenario: Stale edges are removed on rebuild

- **WHEN** a previously imported file `b.py` is no longer imported after an edit, and analysis is re-run
- **THEN** the former `IMPORTS` edge from importer to `b.py` is removed

#### Scenario: Repository isolation

- **WHEN** two repositories each contain a file `main.py` importing `lib.py`
- **THEN** each repository's import edges are scoped to its own `File` nodes and do not cross

### Requirement: Parse-failure isolation and diagnostics

The system SHALL isolate per-file parse failures so that one file's syntax error, encoding issue, or I/O error does not abort analysis of remaining files, and SHALL record failures for diagnostics.

#### Scenario: Syntax error does not abort run

- **WHEN** one file contains a syntax error and other files are valid
- **THEN** analysis completes for the valid files, the failing file is counted in `files_failed`, and a `ParseFailure` with path and reason is recorded

#### Scenario: Encoding or I/O error captured

- **WHEN** a file cannot be read due to encoding or permission issues
- **THEN** the failure is recorded with reason and the file is counted as failed without raising to the caller

### Requirement: Unresolved import diagnostics and counters

The system SHALL produce analysis diagnostics including counts of files scanned/parsed/failed, imports extracted/resolved/unresolved/filtered, and lists of parse failures and unresolved import details (optionally per language).

#### Scenario: Diagnostics counters accurate

- **WHEN** a multi-language repository has 5 files, 2 fail to parse, 10 imports extracted, 6 resolve intra-repo, 2 are stdlib, 2 are unresolved
- **THEN** diagnostics report `files_scanned=5`, `files_parsed=3`, `files_failed=2`, `imports_extracted=10`, `imports_resolved=6`, `imports_filtered_external=2`, `imports_unresolved=2`

#### Scenario: Unresolved reason surfaced

- **WHEN** an import cannot be resolved (missing module, beyond-top-level, unknown language)
- **THEN** the unresolved entry includes importer path, language, raw import text, and reason

### Requirement: Documented limitations

The system SHALL document that dynamic imports, language-specific namespace/package without entry files, generated modules not present on disk, and absence of a static edge not proving absence of a runtime dependency are limitations of static analysis.

#### Scenario: Limitations documented

- **WHEN** a consumer reads the analysis docs or API response
- **THEN** the limitations above are stated, and the system does not claim completeness for dynamic imports

