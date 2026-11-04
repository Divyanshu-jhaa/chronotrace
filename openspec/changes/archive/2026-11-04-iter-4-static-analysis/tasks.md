# Tasks: Iteration 4 — Static Import Analysis (tree-sitter, multi-language)

## 1. Scaffolding and module structure

- [x] 1.1 Create `src/chronotrace/core/static_analysis/` package with `__init__.py`, `models.py`, `language.py`, `scanner.py`, `parser.py`, `mapper.py`, `resolver.py`, `service.py`, add `tree-sitter` + `tree-sitter-language-pack` to `pyproject.toml`, and verify `python -c "import chronotrace.core.static_analysis; import tree_sitter"` succeeds
- [x] 1.2 Define dataclasses in `models.py` (`RawImport` with `language`, `ParseResult`, `ParseFailure`, `ResolvedImport`, `UnresolvedImport`, `Diagnostics` with per-language counters, `AnalysisResult`) and verify `mypy` or import check passes for the module
- [x] 1.3 Implement `language.py` registry (`EXTENSION_MAP`, `LANGUAGE_QUERIES`) detecting language from path and returning `tree_sitter.Language` + query, and verify unit test for `.py/.js/.ts/.java/.go` detection returns expected language

## 2. Source discovery (scanner)

- [x] 2.1 Implement `discover_source_files(repo_root, exclude_patterns, enabled_languages)` with `Path.rglob` per extension, POSIX canonicalization, and glob-based exclusion handling entry files (`__init__.py`, `index.js`, `package.json`) and verify unit test scanning a fixture dir with mixed languages returns expected set on Windows-style and POSIX paths
- [x] 2.2 Add exclusion-pattern tests for `tests/**`, `node_modules/**`, `.*`, `__pycache__/**` and verify excluded files are not returned

## 3. Tree-sitter parsing without execution

- [x] 3.1 Implement `parse_file(path, language)` using `tree_sitter.Parser` + language-specific query extracting import-like constructs into `RawImport(language, module, names, alias, is_relative, lineno)` handling `utf-8-sig`/`surrogateescape` and verify unit tests parsing Python (`import os`, `from foo import bar`, `from . import x`), JS/TS (`import {x} from "./utils"`, `require("../lib")`), Java (`import com.foo.Bar`) return correct records without executing side effects
- [x] 3.2 Implement per-file error isolation (tree-sitter `ERROR` nodes, `UnicodeError`, `OSError` -> `ParseFailure` with `language`) and verify a fixture repo with one broken file per language still parses remaining files and `parse_failures` is populated

## 4. Language-aware module-to-file mapping

- [x] 4.1 Implement `build_module_map(files)` mapping per language (Python `pkg/__init__.py -> pkg`, `pkg/mod.py -> pkg.mod`; JS `pkg/index.js -> pkg`; Java `com/foo/Bar.java -> com.foo.Bar`; Go `pkg/file.go -> pkg/file`) and verify unit test with `app.py`, `pkg/__init__.py`, `pkg/sub.py`, `src/index.js`, `com/foo/Bar.java` yields expected map
- [x] 4.2 Add handling for nested packages and package-import resolution and verify `import pkg` (Python) and `import "pkg/sub"` (JS/Go) resolve to entry files in resolver tests

## 5. Import resolution (language-specific)

- [x] 5.1 Implement absolute/bare resolution against language-aware map with entry-file fallback and verify `a.py -> import b`, `main.js -> import "pkg/sub"`, `Main.java -> import com.foo.Bar` resolve to expected targets
- [x] 5.2 Implement relative resolution per language (Python level arithmetic with `__init__.py` package case; JS `./`/`../` with extension/index fallback) and verify `pkg/a.py: from . import b` (Python) and `src/app.js: import x from "./utils"` (JS) and `pkg/__init__.py: from .sub import x` resolve correctly
- [x] 5.3 Handle beyond-top-level and unresolvable cases (Python `level > depth`, JS `../../` beyond root, missing map entry) classifying as `unresolved` with reason (`beyond_top_level`, `module_not_found`) and verify top-level `from .. import x` (Python) and `from "../../missing"` (JS) yield expected reasons
- [x] 5.4 Implement alias handling (`import foo as bar`, `import {x as y}`) uses real specifier for resolution and verify alias cases resolve to the real module path

## 6. Stdlib and external filtering

- [x] 6.1 Implement per-language filtering using `sys.stdlib_module_names` for Python and builtin sets for JS/TS (`fs`, `path`), Java (`java.*`), Go (stdlib manifest) classifying stdlib and non-mapped third-party as `filtered_external` and verify `os`, `sys`, `json`, `react`, `lodash`, `java.util.*` imports are filtered and produce no resolution
- [x] 6.2 Verify no `File` nodes are created for external modules — integration check asserts `IMPORTS` edges only for intra-repo targets across languages

## 7. Graph persistence (import graph repository)

- [x] 7.1 Implement `ImportGraphRepository.replace_imports(repository_id, edges)` with parameterized `DELETE` + `UNWIND ... MERGE` Cypher (optionally storing `language` on relationship), batched writes, and scoped by `repository_id`/`canonical_path` and verify against a disposable Neo4j (or fake driver double) that edges persist and are scoped per repository
- [x] 7.2 Verify rebuild idempotency: running `replace_imports` twice with same edge set yields one edge per pair, and removing an edge from input removes it from the graph

## 8. Orchestration service and diagnostics

- [x] 8.1 Implement `analyze_repository(repo_root, repository_id, graph, exclude_patterns, enabled_languages)` orchestrating scan -> detect -> parse -> map -> resolve -> persist and returning `AnalysisResult` with `Diagnostics` (including per-language counters) and verify unit test with in-memory fake graph returns correct counts (`files_scanned`, `files_parsed`, `files_failed`, `imports_extracted`, `imports_resolved`, `imports_unresolved`, `imports_filtered_external`)
- [x] 8.2 Wire path normalization to match ingestion canonicalizer and verify Windows/POSIX fixture produces identical graph edges across languages
- [x] 8.3 Add diagnostics detail: `parse_failures[]` and `unresolved_details[]` include `language`, path, raw import text, and reason, and verify diagnostics content in unit tests

## 9. Integration and fixture coverage

- [x] 9.1 Create fixture repositories per language (Python + JS/TS + Java/Go) with mixed absolute, relative, package, stdlib, third-party, and one syntax-error file each and verify `analyze_repository` produces the expected file-level import graph (the iteration completion gate)
- [x] 9.2 Add relative-import and package-import integration tests and verify they pass without creating false edges for stdlib/third-party across languages
- [x] 9.3 Add parse-failure and rebuild tests (re-run updates without duplicating edges, stale edges removed) and verify they pass

## 10. Documentation and limitations

- [x] 10.1 Document tree-sitter multi-language limitations (dynamic imports per language, namespace packages without entry file, generated modules, `package.json` exports, absence of edge not proving no dependency) in `docs/static-analysis.md` or per-package `README` section and verify the documented claims match the spec's limitations requirement
- [x] 10.2 Run `openspec validate --strict` for this change and `pytest` for affected tests and verify both pass with no strict violations (requirement descriptions <=500 chars, scenarios have WHEN/THEN, Purpose >=50 chars)
