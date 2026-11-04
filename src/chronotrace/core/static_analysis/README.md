# Static Analysis — Tree-sitter Multi-language Import Graph

Builds the file-level structural dependency layer for ChronoTrace via tree-sitter without execution.

## Discovery

`discover_source_files(repo_root, exclude_patterns=None, enabled_languages=None)` scans by extension per language (`*.py`, `*.js/.jsx/.mjs/.cjs`, `*.ts/.tsx`, `*.java`, `*.go`, `*.rs`, `*.c/.h`, `*.cpp/.cc/.hpp` etc.). Default `enabled_languages` is `python, javascript, typescript, java, go, rust`. Each candidate is canonicalized via `relative_to(repo_root).as_posix()` → `posixpath.normpath` (matches ingestion `normalize_path`) and filtered by `is_excluded` (gitwildmatch via ingestion filtering, pathspec/fnmatch fallback) for patterns like `tests/**`, `node_modules/**`, `.*`, `__pycache__/**`. Entry files (`__init__.py`, `index.js/ts`, `package.json` not indexed as source) are included via extension match; sorting by canonical POSIX makes Windows/POSIX identical.

Backward compatible `discover_python_files` wraps `discover_source_files(enabled_languages={"python"})`.

## Parsing (tree-sitter)

`parse_file(path, language=None)` detects language via `EXTENSION_MAP` (strict, `.txt` → None), returns `ParseFailure(reason="unsupported_language")` for unknown. Reads file `utf-8-sig`/`surrogateescape`, encodes to `bytes` for `tree_sitter.Parser(Language)`. Uses `tree_sitter_language_pack.get_language` with offline fallback to `tree_sitter_python` etc. via `Language(capsule)`. Parses via `Parser(Language)` or `Parser(); parser.language=` fallback. If `root.has_error` is True (bool, not callable) or `sexp` contains `ERROR`, returns `ParseFailure(language, lineno, SyntaxError: tree-sitter parse error)`. Otherwise extracts imports via language-specific `Language` + `Query` captures:

| Language | Query captures | Extraction |
|---|---|---|
| python | `import_statement` `@imp`, `import_from_statement` `@from` | `import os` per-alias, `from foo import bar` with level, `from . import x` level 1 |
| javascript/typescript | `import_statement` `source: (string) @spec`, `call_expression` `require` | specifier, `is_relative` if startswith ".", `require("../lib")` |
| java | `import_declaration` | `com.foo.Bar` |
| go | `import_declaration` `import_spec` | `fmt`, `github.com/foo/bar` (block and single) |
| rust | `use_declaration` | `std::collections`, `crate::mod` |

All without `exec`/`eval`. Fallback regex (`_fallback_parse`) covers offline or query failures. Returns `ParseResult(path, raw_imports, raw_text, language, success=True)` with `RawImport(language, module, names, asnames, level, lineno, raw_text, is_relative)`.

## Module Map

`build_module_map(files: list[str|Path], repo_root=None) -> dict[str,str]` language-aware, deterministic sorted:

- python: `pkg/__init__.py → pkg`, `pkg/mod.py → pkg.mod`, `top.py → top`
- javascript/typescript: `src/index.js → src` and `pkg/sub/index.js → pkg.sub` + `pkg/sub`, regular files `src/utils.js → src.utils` + `src/utils` (dual dot/slash for bare imports)
- java: `com/foo/Bar.java → com.foo.Bar`
- go: `pkg/file.go → pkg/file` + `pkg.file`, with `go.mod` prefix `module github.com/org/proj` → prefixed keys
- fallback: strip extension, `/` → `.`

Skips `go.mod`, `package.json`.

## Resolution

`resolve_import(raw, importer_module, importer_is_package, module_map, stdlib_names, repo_tops=None, language=None) -> (target, reason) | (target, classification, reason)` per language:

- python: level arithmetic (`package_parts`, `level > len+1 → beyond_top_level`, candidate building, `_lookup_candidate`, `from X import Y` submodule fallback `candidate.Y`)
- javascript/typescript: `is_relative` via `raw.is_relative` or specifier `"."`, importer dir via `_derive_importer_dir`, `posixpath.normpath(join)`, beyond if `..` escapes root, extension/index fallback `.js/.ts/.jsx/.tsx/.mjs/.cjs` and `index.*`
- java: bare package direct lookup, `java.`/`javax.` stdlib
- go: bare import path with quoted stripping, relative `./` via importer dir
- fallback: absolute lookup

`_ReasonStr` aliases make `stdlib` == `filtered_external` for service string checks. `get_stdlib_names(language)` per-language: python `sys.stdlib_module_names`, js `JS_BUILTINS` (fs/path/http + node: prefix, 40+), java `JAVA_STDLIB_TOPS`, go `GO_STDLIB`. `KNOWN_THIRD_PARTY` includes python `requests/neo4j/...` + js `react/lodash/express/...`.

## Filtering

Resolver classifies: 1) `resolved` if in map, 2) `top in stdlib` → `filtered_external:stdlib`, 3) `top not in map and top in KNOWN_THIRD_PARTY` → `filtered_external`, else `unresolved` (with `beyond_top_level`). Service `_is_third_party` only counts `resolved` edges for persistence; `filtered_external`/`unresolved` never create `File` nodes or `IMPORTS` edges.

## Persistence

`ImportGraphRepository.replace_imports(repository_id, edges, batch_size=None)` in `src/chronotrace/graph/repositories/import_repo.py` (canonical) with shims at `core/graph/import_graph.py` and `graph/repositories/import_graph.py`. Accepts `list[tuple[source,target] | tuple[source,target,language] | dict]`, deduplicates `(source,target)` (last language wins), normalizes via `posixpath.normpath`, resolves `batch_size` from param > `driver.batch_size` > 500. Cypher: `MATCH (src:File {repository_id:$repo})-[r:IMPORTS]->() DELETE r` then `UNWIND $edges AS e MATCH (s:File {repository_id:$repo}) WHERE s.canonical_path=e.source OR s.path=e.source MATCH (t:...) MERGE (s)-[r:IMPORTS {repository_id:$repo}]->(t) ON CREATE SET r.language=e.language ON MATCH SET r.language=coalesce(e.language,r.language)` batched. Repository-scoped, idempotent, stale edges removed on rebuild. Fake-driver fallback `_fake_import_edges` for unit tests. `GraphGateway.replace_imports` delegates.

## Orchestration

`analyze_repository(repo_root, repository_id, graph, exclude_patterns=None, enabled_languages=None)` defaults `enabled_languages` to `python, javascript, typescript, java, go`. Orchestrates `discover_source_files` → canonical map via `canonical_path` + `normalize_path` → `build_module_map` → per-file `detect_language` → `parse_file` (expanding multi-name `RawImport` into per-alias singles) → per-language `get_stdlib_names(language)` → `_resolve_single` with `repo_tops` reclassification → `unresolved` vs `filtered_external` → deduplicate sorted edges → `graph.replace_imports`. Returns `AnalysisResult` with `Diagnostics(files_scanned, files_parsed, files_failed, imports_extracted, imports_resolved, imports_unresolved, imports_filtered_external, per_language, parse_failures[], unresolved_details[])` where each `UnresolvedImport`/`ParseFailure` includes `language`, path, raw_text, reason.

## Limitations (spec requirement)

- Dynamic imports per language (`importlib.import_module(var)`, `require(variable)`, `__import__(var)`, `eval("import ...")`, Java reflection, Go `plugin.Open`) with non-static specifiers are recorded as `unresolved` with reason `module_not_found`, never resolved.
- Namespace packages without entry file (PEP 420 dir sans `__init__.py`, JS dir without `index.js`, `package.json` `exports` field) are `unresolved` with reason `namespace_package` or `package_json_exports`; future resolver may read `package.json`/`go.mod`.
- Generated or built modules not present on disk (e.g., `build/`, `dist/`, `node_modules` excluded, `__pycache__`) are not mapped; absent edge does not prove no runtime dependency.
- `package.json` `exports`/`browser` mappings, TypeScript path aliases (`tsconfig paths`), Java class-path wildcards, Go replace directives are not interpreted.
- Tree-sitter grammar version skew may miss exotic syntax; queries are version-tolerant with regex fallback.
- Cross-language bare imports (JS `import "pkg/sub"` where `pkg/sub.py` exists) are unresolved (language-scoped map); only intra-language or JS dual-alias bare imports resolve.
