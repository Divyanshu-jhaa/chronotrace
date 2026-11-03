# Design: Iteration 4 — Static Python Import Analysis

## Context

After Iteration 3, ChronoTrace has a cloned repository on disk, a Neo4j graph with `Repository`, `Commit`, `File`, and `Author` nodes, and canonical POSIX-relative path rules shared with ingestion. No structural dependency layer exists yet.

Iteration 4 introduces `src/chronotrace/core/static_analysis/` and an import-graph repository in `src/chronotrace/core/graph/`. It reuses the Iteration 2 driver lifecycle (application-level `Driver` reuse, parameterized Cypher, `MERGE`/`MATCH`, session/transaction hygiene) and Iteration 3 path conventions. Neo4j stores the current snapshot's file-level `IMPORTS` edges; historical `CO_CHANGES` edges remain untouched.

See `proposal.md` — Why for motivation and `specs/static-analysis/spec.md` for normative requirements; this doc covers *how*.

Constraints:
- Must not execute repository code (AST only).
- Python 3.12+, `>=3.10` provides `sys.stdlib_module_names`.
- No new runtime dependencies beyond stdlib (`ast`, `pathlib`, `sys`, `importlib.util` helpers).
- Repository isolation: every write scoped by `repository_id` (stable ID from Iteration 3).
- Idempotent rebuild: re-running on the same snapshot must not duplicate edges.

## Goals / Non-Goals

**Goals:**
- Discover `*.py` files under a repository root, respecting exclusion patterns and canonical path normalization.
- Parse each file with `ast.parse` without execution, extracting `Import` and `ImportFrom` with level/name/alias/lineno.
- Build an accurate module-to-file map handling packages (`__init__.py`), flat modules, and nested packages.
- Resolve absolute and relative imports to repository files; ignore stdlib/third-party.
- Persist only resolved intra-repository `IMPORTS` edges, with rebuild semantics (delete stale then `MERGE` fresh).
- Surface diagnostics: per-file parse failures, per-import unresolved reasons, aggregate counters.
- Keep the subsystem independently testable with fixture repos (no live Neo4j required for unit tests).

**Non-Goals:**
- Dynamic import resolution (`importlib.import_module(var)`, `__import__`, `exec`).
- Full namespace-package (PEP 420) or editable-install `.pth` support — documented as best-effort.
- Single-file incremental diffing — MVP rebuilds the whole import graph per analysis run.
- Cross-language imports or generated/compiled modules.
- Caching or watch mode.
- LLM-based import inference.

## Decisions

### 1) Module `core/static_analysis` decomposition

**Choice:** Four focused modules + service orchestrator.

| Module | Responsibility |
|---|---|
| `scanner.py` | Walk repo root, yield `*.py` paths, apply exclusion globs, normalize to POSIX-relative, identify `__init__.py` packages |
| `parser.py` | Read file (utf-8 with `errors='surrogateescape'`), `ast.parse`, extract `RawImport` records; catch `SyntaxError`/`UnicodeError`/`OSError` per file |
| `mapper.py` | Build `module_name -> file_path` map: `pkg/__init__.py` => `pkg`, `pkg/mod.py` => `pkg.mod`, `mod.py` => `mod` |
| `resolver.py` | Resolve `RawImport` to target `file_path` via map; handle absolute and relative levels; consult stdlib set to classify `filtered_external` vs `unresolved_internal` |
| `service.py` | Orchestrates scan -> parse -> map -> resolve -> collect diagnostics -> call graph repository `replace_imports(repo_id, edges)` |
| `models.py` | Dataclasses: `RawImport`, `ResolvedImport`, `ParseResult`, `AnalysisResult`/`Diagnostics` |

**Alternatives:** Single 1k-line service. Rejected — resolver and parser have orthogonal test strategies and failure modes.

### 2) AST-only, no execution

**Choice:** `ast.parse` over `importlib` execution or `pyright` analysis.

**Rationale:** Satisfies non-negotiable "must not execute repository code" (PLAN §2.2). Deterministic, stdlib-only, fast. Sufficient for static `import`/`from` statements which cover the product's current needs.

**Alternatives:** Execute with isolated import hook or use `libcst`. Rejected — execution risk and extra dependency; `libcst` adds weight without needed semantics.

### 3) Relative import algorithm

**Choice:** Compute importer package from its module name:
- If importer is `pkg/__init__.py` (module `pkg`), package = `pkg`.
- If importer is `pkg/mod.py` (module `pkg.mod`), package = `pkg` (parent).
- Then for `from .x import y` with `level=N`, walk `N-1` parents up from package, append absolute module tail, look up in map. For `from . import x` (module=None), each imported name `x` is a submodule `package.x` or `parent.x`.

Include `__init__.py` fallback: if resolved name maps to a package directory, prefer its `__init__.py` target.

**Alternatives:** Use `importlib._resolve_name` internals. Rejected — internal API, not stable.

### 4) Stdlib / third-party filtering

**Choice:** Two-tier classification:
1. If module name (top-level segment) not in `mapper` AND in `sys.stdlib_module_names` -> `filtered_external (stdlib)`.
2. If not in mapper and not stdlib -> `filtered_external (third_party/unresolved)` — still not persisted, but counted as `unresolved` for diagnostics unless strict stdlib match.
3. Explicit `stdlib_set = sys.stdlib_module_names` (Python 3.10+), with fallback hard-coded set for older runtimes (not needed on 3.12).

**Alternatives:** Maintain curated stdlib list only. Rejected — drifts with Python releases. Alternative of querying installed packages — unnecessary and slow.

### 5) Import graph persistence — replace semantics

**Choice:** `replace_imports(repository_id, edges)` in graph layer:
```cypher
MATCH (src:File {repository_id: $repo})-[r:IMPORTS]->() DELETE r
UNWIND $edges AS e
MATCH (s:File {repository_id: $repo, canonical_path: e.source})
MATCH (t:File {repository_id: $repo, canonical_path: e.target})
MERGE (s)-[:IMPORTS]->(t)
```
Batched `UNWIND` with configurable batch size (reuse ingestion batch config). All params parameterized.

**Rationale:** Current-snapshot semantics: import graph reflects HEAD, not history. Idempotent; re-run converges. Uses existing `File` identity (repository-scoped + canonical path).

**Alternatives:**
- `MERGE` without delete — leaves stale edges after file edits. Rejected.
- Timestamped edges with TTL — over-engineered for MVP.

**Edges only for resolved intra-repo imports.** No nodes created for external modules.

### 6) Error isolation and diagnostics

**Choice:** Per-file try/except around `open` + `ast.parse`. Failures recorded as `ParseFailure(path, reason, line?)`, not raised. Unresolved imports recorded as `UnresolvedImport(importer, raw, reason)`. `AnalysisResult` carries `Diagnostics { files_scanned, files_parsed, files_failed, imports_extracted, imports_resolved, imports_unresolved, imports_filtered_external, parse_failures[], unresolved_details[] }`.

**Rationale:** Satisfies "parse failures do not abort analysis" and "diagnostics available".

### 7) Path normalization

**Choice:** Reuse ingestion canonicalizer: `path.relative_to(repo_root).as_posix()`. Scanner ensures case-sensitive handling matches ingestion. Exclusion tested against POSIX form on all OS.

### 8) Public interface

```python
# service.py
@dataclass class AnalysisResult:
    repository_id: str
    files_scanned: int
    files_parsed: int
    files_failed: int
    imports_extracted: int
    imports_resolved: int
    relations_persisted: int
    diagnostics: Diagnostics

def analyze_repository(
    repo_root: Path,
    repository_id: str,
    graph: ImportGraphRepository,  # injected, testable with fake
    exclude_patterns: list[str] | None = None,
) -> AnalysisResult: ...

# also:
def discover_python_files(repo_root, exclude_patterns) -> list[Path]
def parse_file(path) -> ParseResult
def build_module_map(files) -> dict[str, str]  # module -> posix path
def resolve_import(raw, importer_module, module_map, stdlib_names) -> ResolveOutcome
```

Graph repository is injected for testability; unit tests use in-memory fake.

## Risks / Trade-offs

- **Syntax errors / non-UTF8 files halt scan** → Mitigation: per-file except, `surrogateescape`/`utf-8-sig`, record and continue.
- **Namespace packages (no `__init__.py`) missed** → Document as limitation; resolver returns `unresolved` with reason `namespace_package`; future resolver can detect PEP 420 directories.
- **Relative import beyond top-level (`from ... import`)** → Detect `level > package_depth + 1`, classify as `unresolved (beyond_top_level)`.
- **Alias (`import foo as bar`) confusion** → `RawImport` keeps both `name` and `asname`; resolution uses real module `name`.
- **Large repos (10k files) — O(N) parse cost** → Mitigation: simple stateless scan, no caching yet; acceptable for MVP snapshot. Batched graph writes avoid per-edge round-trips.
- **Stale edges if analysis skipped** → Caller (ingestion pipeline / API) must invoke `analyze_repository` after each successful checkout; design leaves scheduling to caller.
- **Stdlib list version skew** → Use `sys.stdlib_module_names` from runtime; fallback set is conservative.
- **Circular imports** → Not a resolver problem; edges are directed, cycles are valid and stored.

## Migration Plan

No data migration required for green-field `IMPORTS` relationship (new type). Deploy steps:
1. Ship `static_analysis` + `ImportGraphRepository` code.
2. Run `analyze_repository` for each existing ingested repository (idempotent backfill).
3. Subsequent ingestion runs include the `WRITING_GRAPH` stage that calls analysis after checkout.
4. Rollback: delete `(:File)-[:IMPORTS]->` relationships `WHERE repository_id = $repo`; no schema constraint change to revert.

## Open Questions

- **Shall analysis be synchronous in the ingestion pipeline or via a background job?** Defaulting to synchronous call from ingestion service after checkout; if UX demands non-blocking, Iteration 8 API can move it async without changing stored model. No spec impact.
- **Should per-file parse failures be persisted to Neo4j or returned transiently?** Transient diagnostics suffice for MVP (returned to caller/logs); persistence can be added if UI needs history.
