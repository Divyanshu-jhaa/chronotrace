# Proposal: Iteration 4 — Static Python Import Analysis

## Why

ChronoTrace has historical change data from Git ingestion but no current structural dependency view. Without a file-level import graph built from the live codebase, analytics cannot distinguish direct imports from coincidental co-changes, and queries for impact or criticality lack structural evidence. Python AST parsing without execution provides a safe, deterministic way to build this structural layer before analytics (Iteration 5).

Iteration 4 delivers the static analysis subsystem that discovers Python files, extracts imports via AST, resolves them to repository files, and persists the resulting import graph in Neo4j.

## What Changes

- **Python source discovery**: Walk the checked-out repository snapshot under the configured repositories directory, discovering `*.py` files (including package `__init__.py`). Apply the same canonical path normalization and exclusion semantics as ingestion.
- **AST parsing without execution**: Parse each file with `ast.parse` (no `exec`/`eval`/`importlib` execution). Extract `import` and `from ... import` statements, capturing module name, imported names, alias, level (absolute vs relative), and line number.
- **Module-to-file mapping**: Build a repository-scoped map from Python module names to file paths, accounting for packages (directories with `__init__.py`), top-level modules, and nested packages.
- **Absolute import resolution**: Resolve absolute imports (e.g., `import foo.bar`) against the module-to-file map.
- **Relative import resolution**: Resolve relative imports (e.g., `from . import x`, `from ..sub import y`) using the importing module's package context and `importlib`-style level arithmetic.
- **`__init__.py` handling**: Treat `__init__.py` as the package module itself; imports of a package resolve to its `__init__.py`; sub-module resolution accounts for package directories.
- **Stdlib / third-party filtering**: Identify and exclude standard-library and third-party imports (no matching entry in module-to-file map, plus stdlib name filtering) so only intra-repository edges are persisted. No false `File` nodes for external modules.
- **Import graph persistence**: Write `(:File)-[:IMPORTS]->(:File)` relationships to Neo4j via the shared graph layer, using stable file identifiers and parameterized Cypher. Rebuild semantics: clear stale import edges for the repository before writing the fresh snapshot, making re-runs idempotent without duplicating edges.
- **Diagnostics and resilience**: Record per-file parse failures (SyntaxError, encoding issues) and per-import unresolved counts. A single file's parse failure does not abort the repository-wide analysis. Expose diagnostics to callers/API.
- **Breakdown/observability**: Provide counters (files scanned, parsed, failed; imports extracted, resolved, unresolved, filtered) suitable for logging and tests.

## Capabilities

### New Capabilities

- `static-analysis`: Static Python import extraction, module-to-file resolution (absolute and relative with `__init__.py` handling), stdlib/third-party filtering, import graph persistence, and diagnostics. Owns the file-level `IMPORTS` relationship lifecycle.

### Modified Capabilities

- None — no existing spec capabilities are modified. Iterations 0–3 established foundation, config, graph, and ingestion; static analysis is additive.

## Impact

- **New module**: `src/chronotrace/core/static_analysis/` — scanner, parser, resolver, service, diagnostics models.
- **Graph layer extension**: New import-graph repository / Cypher for `IMPORTS` relationships (merge/delete per repository). Reuses driver lifecycle, session management, and parameterized Cypher conventions from Iteration 2.
- **Ingestion coordination**: Ingestion pipeline (Iteration 3) calls static analysis as a post-checkout step to rebuild the import graph from the current snapshot.
- **Dependencies**: Python standard library `ast`, `pathlib`, `sys.stdlib_module_names` (Python 3.10+), `importlib` helpers for resolution logic; no new external packages required.
- **Neo4j**: New `IMPORTS` relationship type on existing `File` nodes; uniqueness/idempotency via `MERGE` on stable file IDs.
- **Configuration**: Uses existing filtered-path and repository-root settings; `pyproject.toml` / `chronotrace.yml` analysis settings may expose future tuning (not required for MVP).
- **Out of scope (explicitly excluded)**:
  - Dynamic imports (`importlib.import_module` with variables, `__import__`, `exec`): not resolved.
  - Namespace packages (PEP 420, no `__init__.py`): best-effort or documented limitation.
  - Generated or built modules not present on disk: not mapped.
  - Cross-language or non-Python files: ignored.
  - LLM or heuristic import inference: out of scope.

## Acceptance Criteria (Iteration-level)

- See PLAN.md Iteration 4 — scope, limitations, and completion gate apply; summarized here for traceability:
  - Discovered `*.py` files are parsed via AST without execution.
  - Absolute and relative imports resolve correctly; package `__init__.py` semantics honored.
  - Third-party/stdlib imports do not create false repository `File` nodes.
  - Parse failures are isolated per file; unresolved imports and failures are counted/exposed.
  - Re-running analysis updates the current import graph without duplicating edges.
  - A fixture repository produces the expected file-level import graph (completion gate).

## Dependencies and Sequencing

- Depends on: Iteration 2 (graph schema, driver, `File` identity) and Iteration 3 (repository checkout on disk, canonical path rules, exclusion patterns).
- Unblocks: Iteration 5 (analytics — import fan-in, historical+structural impact, deterministic scores).
