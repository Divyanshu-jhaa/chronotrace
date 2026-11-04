# Design: Iteration 4 — Static Import Analysis (tree-sitter, multi-language)

## Context

After Iteration 3, ChronoTrace has a cloned repository on disk, a Neo4j graph with `Repository`, `Commit`, `File`, and `Author` nodes, and canonical POSIX-relative path rules shared with ingestion. No structural dependency layer exists yet.

Iteration 4 introduces `src/chronotrace/core/static_analysis/` and an import-graph repository in `src/chronotrace/core/graph/`. It reuses the Iteration 2 driver lifecycle (application-level `Driver` reuse, parameterized Cypher, `MERGE`/`MATCH`, session/transaction hygiene) and Iteration 3 path conventions. Neo4j stores the current snapshot's file-level `IMPORTS` edges; historical `CO_CHANGES` edges remain untouched.

See `proposal.md` — Why for motivation and `specs/static-analysis/spec.md` for normative requirements; this doc covers *how*.

Constraints:
- Must not execute repository code (tree-sitter parsing only, no `exec`/`eval`/`import`).
- Python 3.12+; `tree-sitter` + `tree-sitter-language-pack` (or per-language grammars: `tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript`, `tree-sitter-java`, `tree-sitter-go`, `tree-sitter-rust`, …).
- Repository isolation: every write scoped by `repository_id` (stable ID from Iteration 3).
- Idempotent rebuild: re-running on the same snapshot must not duplicate edges.
- Language-agnostic: same `IMPORTS` relationship type across languages; optional `language` property on relationship/file for filtering.

## Goals / Non-Goals

**Goals:**
- Discover source files for all enabled languages under a repository root, respecting exclusion patterns and canonical path normalization.
- Parse each file with tree-sitter (language-specific `Language` + `Parser` + query) without execution, extracting import-like constructs with specifier/names/alias/lineno/language.
- Build accurate language-aware module-to-file maps handling package entry files (`__init__.py`, `index.js`, `package.json`, Java package dirs, `go.mod` module path) and nested packages.
- Resolve absolute/bare and relative imports to repository files per language; ignore stdlib/external.
- Persist only resolved intra-repository `IMPORTS` edges, with rebuild semantics (delete stale then `MERGE` fresh).
- Surface diagnostics: per-file parse failures, per-import unresolved reasons, aggregate counters (optionally per language).
- Keep the subsystem independently testable with fixture repos (no live Neo4j required for unit tests).

**Non-Goals:**
- Dynamic import resolution with non-static specifiers (`importlib.import_module(var)`, `require(variable)`, `__import__(var)`, `exec`).
- Full namespace-package (PEP 420 sans `__init__.py`) or editable-install `.pth` support — documented as best-effort.
- Single-file incremental diffing — MVP rebuilds the whole import graph per analysis run.
- Caching, watch mode, or LLM-based import inference.
- Exhaustive support for every exotic grammar on day one — MVP covers Python + JS/TS + Java + Go; others incremental via grammar pack.

## Decisions

### 1) Module `core/static_analysis` decomposition

**Choice:** Five focused modules + service orchestrator.

| Module | Responsibility |
|---|---|
| `scanner.py` | Walk repo root, yield source paths per enabled language, apply exclusion globs, normalize to POSIX-relative, identify entry files (`__init__.py`, `index.js`, `mod.rs`) |
| `language.py` | Detect language from extension/path, return `Language` + tree-sitter query string; registry extensible (`EXTENSION_MAP`, `LANGUAGE_QUERIES`) |
| `parser.py` | Load `tree_sitter.Parser(Language)`, run query captures, extract `RawImport(language, module, names, alias, is_relative, lineno, col)`; catch parse/encoding/OSError per file |
| `mapper.py` | Build `specifier -> file_path` map per language: Python `pkg/__init__.py => pkg`, JS `pkg/index.js => pkg`, Java `com/foo/Bar.java => com.foo.Bar`, Go `pkg/file.go => pkg/file` |
| `resolver.py` | Resolve `RawImport` to target `file_path` via language-aware map; handle bare vs relative, entry-file fallback; consult per-language stdlib/builtin sets to classify `filtered_external` vs `unresolved_internal` |
| `service.py` | Orchestrates scan -> detect -> parse -> map -> resolve -> collect diagnostics -> call graph repository `replace_imports(repo_id, edges)` |
| `models.py` | Dataclasses: `RawImport`, `ResolvedImport`, `ParseResult`, `AnalysisResult`/`Diagnostics` |

**Alternatives:** Single 1k-line service. Rejected — resolver and parser have orthogonal test strategies and failure modes. Keeping language detection separate keeps queries testable.

### 2) Tree-sitter over `ast` / per-language parsers

**Choice:** `tree-sitter` + `tree-sitter-language-pack` (or individual `tree-sitter-*` packages) as the single parsing substrate.

**Rationale:** Satisfies "must not execute repository code" uniformly across languages. Single API (`Parser`, `Language`, `Query`) vs fragmented `ast`+`espree`+`javaparser`. Incremental parsing, error recovery (tree-sitter tolerates syntax errors and still yields captures for remaining imports), deterministic, no execution. Sufficient for static import-like statements which cover the product's current needs. `language-pack` bundles common grammars without N native builds.

**Alternatives:** `ast.parse` for Python plus `espree`/`javaparser` per language. Rejected — fragmented error handling, different AST shapes, more deps, no uniform query language. Alternative of using language servers (pyright/tsserver) — heavy, execution-adjacent. Alternative of executing with isolated import hook — violates non-negotiable constraint.

**Queries (examples):**
- Python: `(import_statement) @imp`, `(import_from_statement module_name: (_) @mod) @from`
- JS/TS: `(import_statement source: (string) @spec) @imp`, `(call_expression function: (identifier) @req (#eq? @req "require") arguments: (arguments (string) @spec))`
- Java: `(import_declaration) @imp`
- Go: `(import_declaration) @imp`
- Rust: `(use_declaration) @imp`, `(mod_item) @mod`

Captures normalized to `RawImport` by language-specific post-processor.

### 3) Relative import algorithm (per language)

**Choice:**
- Python (as before): Compute importer package from its module name; for `level=N`, walk `N-1` parents, append tail, lookup; entry-file fallback.
- JS/TS: Resolve specifiers starting with `.`/`..` relative to importer's directory: `path.resolve(importer_dir, specifier)` with extensions `.js/.ts/.jsx/.tsx`/directory `index.*` fallback; bare specifiers via map.
- Java: Bare package specifiers only (no relative); relative not applicable.
- Go: Import path resolution via `go.mod` module prefix + directory mapping; relative imports rare, treat `./` as relative to importer dir.
- General: If resolved name maps to a package directory, prefer its entry file target.

**Alternatives:** Use `importlib._resolve_name` internals. Rejected — Python-only, internal API, not stable across languages.

### 4) Stdlib / external filtering

**Choice:** Two-tier classification per language:
1. If specifier's top-level segment not in language map AND in language stdlib/builtin set -> `filtered_external (stdlib)` (Python `sys.stdlib_module_names`, JS builtins, Java `java.*`, Go stdlib list).
2. If not in map and not stdlib -> `filtered_external (third_party/unresolved)` — still not persisted, but counted as `filtered_external`/`unresolved` for diagnostics.
3. Explicit per-language sets: Python `sys.stdlib_module_names`, JS `node:fs/path` builtins, Java `java.*`/`javax.*`, Go stdlib manifest, with fallback hard-coded sets.

**Alternatives:** Maintain curated single stdlib list only. Rejected — drifts with releases and misses per-language builtins. Alternative of querying installed packages — unnecessary and slow.

### 5) Import graph persistence — replace semantics

**Choice:** `replace_imports(repository_id, edges)` in graph layer:
```cypher
MATCH (src:File {repository_id: $repo})-[r:IMPORTS]->() DELETE r
UNWIND $edges AS e
MATCH (s:File {repository_id: $repo, canonical_path: e.source})
MATCH (t:File {repository_id: $repo, canonical_path: e.target})
MERGE (s)-[:IMPORTS {language: e.language}]->(t)
```
Batched `UNWIND` with configurable batch size (reuse ingestion batch config). All params parameterized.

**Rationale:** Current-snapshot semantics: import graph reflects HEAD, not history. Idempotent; re-run converges. Uses existing `File` identity (repository-scoped + canonical path).

**Alternatives:**
- `MERGE` without delete — leaves stale edges after file edits. Rejected.
- Timestamped edges with TTL — over-engineered for MVP.

**Edges only for resolved intra-repo imports.** No nodes created for external modules.

### 6) Error isolation and diagnostics

**Choice:** Per-file try/except around `open` + `Parser.parse`. Tree-sitter parse returns tree even with `ERROR` nodes — we still extract captures outside error spans. Failures recorded as `ParseFailure(path, language, reason, line?)`, not raised. Unresolved imports recorded as `UnresolvedImport(importer, language, raw, reason)`. `AnalysisResult` carries `Diagnostics { files_scanned, files_parsed, files_failed, imports_extracted, imports_resolved, imports_unresolved, imports_filtered_external, per_language: dict, parse_failures[], unresolved_details[] }`.

**Rationale:** Satisfies "parse failures do not abort analysis" and "diagnostics available". Tree-sitter's error recovery means partial results even for broken files (unlike `ast`).

### 7) Path normalization and language detection

**Choice:** Reuse ingestion canonicalizer: `path.relative_to(repo_root).as_posix()`. Scanner ensures case-sensitive handling matches ingestion. Exclusion tested against POSIX form on all OS. Language detection via extension map (`{".py": "python", ".js": "javascript", ".ts": "typescript", ".java": "java", ".go": "go", ...}`) with fallback `unknown` -> skipped with diagnostic.

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
    enabled_languages: set[str] | None = None,
) -> AnalysisResult: ...

# also:
def discover_source_files(repo_root, exclude_patterns, enabled_languages) -> list[Path]
def detect_language(path: Path) -> str | None
def parse_file(path: Path, language: str) -> ParseResult
def build_module_map(files: list[Path]) -> dict[str, str]  # specifier -> posix path (per language)
def resolve_import(raw: RawImport, importer_path: Path, module_map, stdlib_names) -> ResolveOutcome
```

Graph repository is injected for testability; unit tests use in-memory fake.

## Risks / Trade-offs

- **Syntax errors / non-UTF8 files halt scan** → Mitigation: per-file except, `surrogateescape`/`utf-8-sig`, tree-sitter error recovery still yields captures, record and continue.
- **Tree-sitter grammar version skew** → Pin `tree-sitter` + `tree-sitter-language-pack` versions; integration tests assert query captures against fixture repos per language.
- **Namespace packages / JS bare specifiers via `package.json` exports** → Document as limitation; resolver returns `unresolved` with reason `namespace_package` or `package_json_exports`; future resolver can read `package.json`.
- **Relative import beyond top-level (`from ... import` in Python)** → Detect `level > package_depth + 1`, classify as `unresolved (beyond_top_level)`.
- **Alias (`import foo as bar`) confusion** → `RawImport` keeps both `name` and `asname`; resolution uses real module `name`/specifier.
- **Large repos (10k files) — O(N) parse cost** → Mitigation: stateless scan, tree-sitter is fast (incremental), no caching yet; acceptable for MVP. Batched graph writes avoid per-edge round-trips.
- **Stale edges if analysis skipped** → Caller (ingestion pipeline / API) must invoke `analyze_repository` after each successful checkout; design leaves scheduling to caller.
- **Stdlib list version skew per language** → Use per-language builtin sets from runtime where available; fallback conservative sets.
- **Circular imports** → Not a resolver problem; edges are directed, cycles are valid and stored.

## Migration Plan

No data migration required for green-field `IMPORTS` relationship (new type). Deploy steps:
1. Ship `static_analysis` + `ImportGraphRepository` code with `tree-sitter` deps.
2. Run `analyze_repository` for each existing ingested repository (idempotent backfill).
3. Subsequent ingestion runs include the `WRITING_GRAPH` stage that calls analysis after checkout.
4. Rollback: delete `(:File)-[:IMPORTS]->` relationships `WHERE repository_id = $repo`; no schema constraint change to revert.

## Open Questions

- **Shall analysis be synchronous in the ingestion pipeline or via a background job?** Defaulting to synchronous call from ingestion service after checkout; if UX demands non-blocking, Iteration 8 API can move it async without changing stored model. No spec impact.
- **Should per-file parse failures be persisted to Neo4j or returned transiently?** Transient diagnostics suffice for MVP (returned to caller/logs); persistence can be added if UI needs history.
- **Which languages to enable by default on first ship?** Propose `python`, `javascript`, `typescript`, `java`, `go` as MVP; `rust`/`c`/`cpp` follow via grammar pack without spec change.
