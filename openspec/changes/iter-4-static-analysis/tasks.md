# Tasks: Iteration 4 — Static Python Import Analysis

## 1. Scaffolding and module structure

- [ ] 1.1 Create `src/chronotrace/core/static_analysis/` package with `__init__.py`, `models.py`, `scanner.py`, `parser.py`, `mapper.py`, `resolver.py`, `service.py` and verify `python -c "import chronotrace.core.static_analysis"` succeeds
- [ ] 1.2 Define dataclasses in `models.py` (`RawImport`, `ParseResult`, `ParseFailure`, `ResolvedImport`, `UnresolvedImport`, `Diagnostics`, `AnalysisResult`) and verify `mypy` or import check passes for the module

## 2. Source discovery (scanner)

- [ ] 2.1 Implement `discover_python_files(repo_root, exclude_patterns)` with `Path.rglob`, POSIX canonicalization, and glob-based exclusion handling `__init__.py` inclusion and verify unit test scanning a fixture dir returns expected `*.py` set on Windows-style and POSIX paths
- [ ] 2.2 Add exclusion-pattern tests for `tests/**`, `.*`, `__pycache__/**` and verify excluded files are not returned

## 3. AST parsing without execution

- [ ] 3.1 Implement `parse_file(path)` using `ast.parse` extracting `Import`/`ImportFrom` into `RawImport(module, names, asnames, level, lineno)` handling utf-8-sig/surrogateescape and verify unit test parsing a file with `import os`, `from foo import bar`, `from . import x` returns correct records without executing side effects
- [ ] 3.2 Implement per-file error isolation (SyntaxError, UnicodeError, OSError -> `ParseFailure`) and verify a fixture repo with one broken file still parses remaining files and `parse_failures` is populated

## 4. Module-to-file mapping

- [ ] 4.1 Implement `build_module_map(files)` mapping `pkg/__init__.py -> pkg`, `pkg/mod.py -> pkg.mod`, `top.py -> top` and verify unit test with `app.py`, `pkg/__init__.py`, `pkg/sub.py`, `pkg/sub/nested.py` yields expected map
- [ ] 4.2 Add handling for nested packages and package-import resolution and verify `import pkg` resolves to `pkg/__init__.py` in resolver tests

## 5. Import resolution (absolute + relative)

- [ ] 5.1 Implement absolute resolution against module map with `__init__.py` fallback and verify `a.py -> import b` and `main.py -> from pkg.sub import foo` resolve to expected targets
- [ ] 5.2 Implement relative resolution using importer package context (level arithmetic, parent walk, `__init__.py` package case) and verify `pkg/a.py: from . import b` and `pkg/sub/mod.py: from .. import utils` and `pkg/__init__.py: from .sub import x` resolve correctly
- [ ] 5.3 Handle beyond-top-level and unresolvable cases (level > depth, missing map entry) classifying as `unresolved` with reason and verify top-level `from .. import x` yields `beyond_top_level` and missing module yields `module_not_found`
- [ ] 5.4 Implement alias handling (`import foo as bar` uses `foo` for resolution) and verify alias case resolves to the real module path

## 6. Stdlib and third-party filtering

- [ ] 6.1 Implement filtering using `sys.stdlib_module_names` (with fallback set) classifying stdlib and non-mapped third-party as `filtered_external` and verify `os`, `sys`, `json`, `requests`, `neo4j` imports are filtered and produce no resolution
- [ ] 6.2 Verify no `File` nodes are created for external modules — integration check asserts `IMPORTS` edges only for intra-repo targets

## 7. Graph persistence (import graph repository)

- [ ] 7.1 Implement `ImportGraphRepository.replace_imports(repository_id, edges)` with parameterized `DELETE` + `UNWIND ... MERGE` Cypher, batched writes, and scoped by `repository_id`/`canonical_path` and verify against a disposable Neo4j (or fake driver double) that edges persist and are scoped per repository
- [ ] 7.2 Verify rebuild idempotency: running `replace_imports` twice with same edge set yields one edge per pair, and removing an edge from input removes it from the graph

## 8. Orchestration service and diagnostics

- [ ] 8.1 Implement `analyze_repository(repo_root, repository_id, graph, exclude_patterns)` orchestrating scan -> parse -> map -> resolve -> persist and returning `AnalysisResult` with `Diagnostics` and verify unit test with in-memory fake graph returns correct counts (`files_scanned`, `files_parsed`, `files_failed`, `imports_extracted`, `imports_resolved`, `imports_unresolved`, `imports_filtered_external`)
- [ ] 8.2 Wire path normalization to match ingestion canonicalizer and verify Windows/POSIX fixture produces identical graph edges
- [ ] 8.3 Add diagnostics detail: `parse_failures[]` and `unresolved_details[]` include path, raw import text, and reason, and verify diagnostics content in unit tests

## 9. Integration and fixture coverage

- [ ] 9.1 Create fixture repository with mixed absolute, relative, package, stdlib, third-party, and one syntax-error file and verify `analyze_repository` produces the expected file-level import graph (the iteration completion gate)
- [ ] 9.2 Add relative-import and package-import integration tests and verify they pass without creating false edges for stdlib/third-party
- [ ] 9.3 Add parse-failure and rebuild tests (re-run updates without duplicating edges, stale edges removed) and verify they pass

## 10. Documentation and limitations

- [ ] 10.1 Document static-analysis limitations (dynamic imports, namespace packages, generated modules, absence of edge not proving no dependency) in `docs/static-analysis.md` or `README` section and verify the documented claims match the spec's limitations requirement
- [ ] 10.2 Run `openspec validate --strict` for this change and `pytest` for affected tests and verify both pass with no strict violations (requirement descriptions <=500 chars, scenarios have WHEN/THEN, Purpose >=50 chars)

