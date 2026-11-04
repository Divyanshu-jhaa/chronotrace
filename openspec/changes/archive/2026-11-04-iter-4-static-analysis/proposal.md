# Proposal: Iteration 4 — Static Import Analysis (tree-sitter, multi-language)

## Why

ChronoTrace has historical change data from Git ingestion but no current structural dependency view. Without a file-level import graph built from the live codebase, analytics cannot distinguish direct imports from coincidental co-changes, and queries for impact or criticality lack structural evidence. Tree-sitter parsing without execution provides a safe, deterministic, language-agnostic way to build this structural layer for all supported languages before analytics (Iteration 5).

Iteration 4 delivers the static analysis subsystem that discovers source files for every supported language, extracts imports via tree-sitter queries, resolves them to repository files, and persists the resulting import graph in Neo4j.

## What Changes

- **Multi-language source discovery**: Walk the checked-out repository snapshot under the configured repositories directory, discovering source files by extension (`*.py`, `*.js`, `*.jsx`, `*.ts`, `*.tsx`, `*.java`, `*.go`, `*.rs`, `*.c`, `*.cpp`, `*.rb`, `*.php`, and extensible others — including package entry points such as `__init__.py`, `index.js`, `mod.rs`). Apply the same canonical path normalization and exclusion semantics as ingestion.
- **Tree-sitter parsing without execution**: Parse each file with a tree-sitter `Parser` and language-specific `Language` + query (no `exec`/`eval`/`import` execution). Extract import-like constructs per grammar: Python `import`/`from ... import`, JavaScript/TypeScript `import ... from`, `require()`, `import()`, Java `import`, Go `import`, Rust `use`/`mod`, etc., capturing raw module specifier, imported names, alias, relative vs absolute form, and line/column.
- **Language-aware module-to-file mapping**: Build a repository-scoped map from logical module specifiers to canonical file paths per language, accounting for packages (Python `__init__.py`, JS `index.js`/`package.json`, Java package directories, Go module paths, nested packages).
- **Absolute import resolution**: Resolve absolute/bare specifiers (e.g., `import foo.bar`, `import {x} from "lib/utils"`, `import com.foo.Bar`) against the language-aware module-to-file map.
- **Relative import resolution**: Resolve relative imports (e.g., Python `from . import x`, `from ..sub import y`; JS `from "./utils"` / `from "../lib"`) using the importing file's package context and language-specific level/path arithmetic.
- **Package entry-point handling**: Treat package entry files (`__init__.py`, `index.js`/`index.ts`, `mod.rs`) as the package module itself; imports of a package resolve to its entry file; sub-module resolution accounts for package directories.
- **Stdlib / external filtering**: Identify and exclude standard-library and third-party imports (no matching entry in module-to-file map, plus language-specific stdlib/builtin sets) so only intra-repository edges are persisted. No false `File` nodes for external modules.
- **Import graph persistence**: Write `(:File)-[:IMPORTS]->(:File)` relationships to Neo4j via the shared graph layer, using stable file identifiers and parameterized Cypher. Rebuild semantics: clear stale import edges for the repository before writing the fresh snapshot, making re-runs idempotent without duplicating edges. Edges carry `language` where useful.
- **Diagnostics and resilience**: Record per-file parse failures (tree-sitter parse errors, encoding issues) and per-import unresolved counts. A single file's parse failure does not abort the repository-wide analysis. Expose diagnostics to callers/API.
- **Breakdown/observability**: Provide counters (files scanned, parsed, failed; imports extracted, resolved, unresolved, filtered — optionally per language) suitable for logging and tests.

## Capabilities

### New Capabilities

- `static-analysis`: Tree-sitter–based multi-language import extraction, language-aware module-to-file resolution (absolute and relative with package entry-point handling), stdlib/external filtering, import graph persistence, and diagnostics. Owns the file-level `IMPORTS` relationship lifecycle.

### Modified Capabilities

- None — no existing spec capabilities are modified. Iterations 0–3 established foundation, config, graph, and ingestion; static analysis is additive.

## Impact

- **New module**: `src/chronotrace/core/static_analysis/` — scanner, language detector, tree-sitter parser (queries per language), resolver, service, diagnostics models.
- **Graph layer extension**: New import-graph repository / Cypher for `IMPORTS` relationships (merge/delete per repository). Reuses driver lifecycle, session management, and parameterized Cypher conventions from Iteration 2.
- **Ingestion coordination**: Ingestion pipeline (Iteration 3) calls static analysis as a post-checkout step to rebuild the import graph from the current snapshot.
- **Dependencies**: `tree-sitter` + `tree-sitter-language-pack` (or individual `tree-sitter-*` grammars) for parsing; Python `pathlib` for paths; language-specific stdlib name sets (Python `sys.stdlib_module_names`, JS/TS builtins, etc.) for filtering. No `ast`-only limitation.
- **Neo4j**: New `IMPORTS` relationship type on existing `File` nodes; uniqueness/idempotency via `MERGE` on stable file IDs.
- **Configuration**: Uses existing filtered-path and repository-root settings; `pyproject.toml` / `chronotrace.yml` analysis settings may expose language enablement and exclusion tuning.
- **Out of scope (explicitly excluded)**:
  - Dynamic imports with non-static specifiers (`importlib.import_module(var)`, `require(variable)`, `__import__(var)`, `exec`): not resolved (recorded as unresolved).
  - Namespace packages without entry file (PEP 420, JS without `index`): best-effort or documented limitation.
  - Generated or built modules not present on disk: not mapped.
  - LLM or heuristic import inference: out of scope.

## Acceptance Criteria (Iteration-level)

- See PLAN.md Iteration 4 — scope, limitations, and completion gate apply; summarized here for traceability:
  - Discovered source files for each enabled language are parsed via tree-sitter without execution.
  - Absolute and relative imports resolve correctly per language; package entry-point semantics honored.
  - Third-party/stdlib imports do not create false repository `File` nodes.
  - Parse failures are isolated per file; unresolved imports and failures are counted/exposed.
  - Re-running analysis updates the current import graph without duplicating edges.
  - A fixture repository per language (at minimum Python + JS/TS, plus one JVM/Go sample) produces the expected file-level import graph (completion gate).

## Dependencies and Sequencing

- Depends on: Iteration 2 (graph schema, driver, `File` identity) and Iteration 3 (repository checkout on disk, canonical path rules, exclusion patterns).
- Unblocks: Iteration 5 (analytics — import fan-in, historical+structural impact, deterministic scores).
