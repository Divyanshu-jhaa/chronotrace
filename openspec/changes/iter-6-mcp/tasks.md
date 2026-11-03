# Tasks: Iteration 6 — MCP Server and Five Tool Contracts

## 1. Scaffolding and dependency setup

- [ ] 1.1 Add `mcp` Python SDK to `pyproject.toml` runtime dependencies (pin version, e.g., `mcp>=1.0`), run `pip install -e ".[dev]"` or `pip install mcp` in dev environment, and verify `python -c "import mcp; print(mcp.__version__)"` succeeds and the server entry `python -m chronotrace.mcp_server.server --help` is importable after creating the package scaffold
- [ ] 1.2 Create `src/chronotrace/mcp_server/` package with `__init__.py`, `server.py`, `health.py`, and `tools/__init__.py`, `tools/validation.py`, `tools/errors.py` stubs that import without errors and wire the validated `chronotrace.yml` graph/analytics config object (consume, do not re-parse YAML/env) and verify unit import test passes
- [ ] 1.3 Document the MCP HTTP transport constant (endpoint path) in `server.py` (e.g., `MCP_HTTP_PATH`) sourced from the installed SDK's streamable-http module and verify the constant matches the SDK's declared path by a unit assertion importing the SDK's transport module

## 2. Shared validation and path safety

- [ ] 2.1 Implement `validate_repository_id(value)` in `tools/validation.py` — require non-empty string, length 1–256, stable-ID alphabet, reject `None`/non-string/whitespace, and verify unit test with empty, `None`, numeric, and unknown-ID cases returning `validation_error` vs `not_found` correctly
- [ ] 2.2 Implement `validate_file_path(value)` with POSIX canonicalization, rejection of absolute paths (`/`, `C:`, `\\`), `..` segment traversal (before and after `as_posix`), backslash, and `\x00`, and verify unit tests cover `src/foo/bar.py` accepted vs `/etc/passwd`, `../secret.py`, `a/../b.py`, `a\b\c.py`, and `nul-byte%00.py` rejected with `validation_error`
- [ ] 2.3 Implement `validate_limit(value, default=20, max_limit=100)` for `get_file_history` — accept only integral ints in `1..max`, default when `None`, reject `0`, negatives, floats, `NaN`, strings, and `> max` with structured `validation_error`, and verify unit tests cover `None` → default, `50` → accepted, `0`/`-1`/`"20"`/`NaN`/`101` → rejected
- [ ] 2.4 Implement `normalize_path(value)` canonical POSIX helper shared with ingestion/static-analysis rules and verify Windows `a/b\c.py` vs POSIX `a/b/c.py` normalization consistency test passes
- [ ] 2.5 Implement `tools/errors.py` structured error mapper — `ValidationError`/`GraphNotAvailable`/`NotFound` → `{status, error: {code, message}, isError: true}` without leaking secrets, raw Cypher, or connection strings, and verify unit test asserting `"' OR 1=1"` payloads are parameterized and error messages contain no credentials

## 3. Graph file-history repository

- [ ] 3.1 Implement `FileHistoryRepository.get_commits_for_file(repository_id, canonical_path, limit)` in `src/chronotrace/core/graph/repositories/` (or `src/chronotrace/graph/`) with parameterized Cypher `MATCH (c:Commit)-[r:MODIFIED]->(f:File {repository_id:$repo, canonical_path:$path}) RETURN c, r ORDER BY c.date DESC LIMIT $limit` plus `total_count`/`has_more` metadata, scoped by `repository_id`, and verify unit test mocking `session.run` asserts parameters dict and query contains `$repo`/`$path`/`$limit` placeholders with no string interpolation
- [ ] 3.2 Verify repository isolation — two repositories with same file path return only the queried repository's commits, and verify integration test against disposable Neo4j with fixture commits and bounded limit passes

## 4. First tool: get_change_criticality (end-to-end slice)

- [ ] 4.1 Implement `tools/criticality.py` handler `get_change_criticality(repository_id, file_path)` calling shared validators then `analytics_service.get_change_criticality(repo_id, path)` and mapping to the common envelope `{status, repository_id, target, score, breakdown, evidence, limitations}` and verify unit test with mocked analytics returns `status: ok` with breakdown components and evidence, plus `insufficient_history` case disclosed explicitly
- [ ] 4.2 Register `get_change_criticality` with the MCP server in `server.py` via `@server.tool()`/`@mcp.tool()` decorator, wire `create_mcp_server(config, graph_gateway, analytics_service)` factory, and verify a throwaway MCP client `list_tools` shows the tool and `call_tool` with valid inputs returns the expected envelope without crashing

## 5. Remaining four tools

- [ ] 5.1 Implement `tools/regression.py` handler `get_regression_risk(repository_id, file_path)` delegating to `analytics.regression`, returning `{status, repository_id, target, fix_commits: [{hash, date, message, fix_reason}], co_changed_files: [{path, count, confidence}]}` ranked and deduplicated, and verify unit test with fixture fix commits asserts ranking and `no fix evidence` message rather than zero-risk claim
- [ ] 5.2 Implement `tools/impact.py` handler `get_historical_impact(repository_id, file_path)` delegating to `analytics.impact`, returning `{status, importers, dependencies, co_changed, potential_hidden_couplings}` with each entry carrying `path`, `relationship_type`, supporting commits, flagging hidden couplings as observed association not proven dependency, and verify unit test with fixture `IMPORTS`+`CO_CHANGES` graph asserts hidden coupling correctly appears in `potential_hidden_couplings`
- [ ] 5.3 Implement `tools/test_reco.py` handler `recommend_tests(repository_id, file_path)` delegating to `analytics.test_reco`, returning ranked `{candidates: [{path, score, rank, reasons}]}` with per-candidate signals and server-side cap (e.g., 20), including `limitations` disclaimer of no real coverage guarantee, and verify unit test with fixture tests by naming/import/co-change asserts correct `reasons` and ordering
- [ ] 5.4 Implement `tools/file_history.py` handler `get_file_history(repository_id, file_path, limit?)` validating all three inputs then calling `FileHistoryRepository.get_commits_for_file`, returning `{status, repository_id, target, commits: [{hash, date, message, author, is_fix, fix_reason, additions, deletions, change_type}], limit_applied, total_count|has_more}`, and verify unit test for bounded `limit: 10` ordered descending, `limit: 0` rejected, and `not_found` vs `validation_error` distinction
- [ ] 5.5 Register the four remaining tools with the MCP server in `server.py` and verify a throwaway MCP client can `list_tools` (five tools present) and call each independently with valid inputs

## 6. MCP server lifecycle, transport, and health

- [ ] 6.1 Implement `create_mcp_server(config)` factory that creates the single shared Neo4j driver/gateway and `AnalyticsService`, injects them into tool closures, and exposes async lifespan/context manager that drains in-flight calls then `driver.close()` on shutdown, and verify unit test asserting two successive tool calls reuse the same driver instance (mock spy) and `close()` is idempotent
- [ ] 6.2 Wire Streamable HTTP transport — `server.serve(host=config.mcp.host, port=config.mcp.port, transport="streamable-http")` (or SDK-idiomatic entry), bind host/port from config defaults, and verify integration test starts server on ephemeral port, separate client connects to the verified `MCP_HTTP_PATH` endpoint, calls `list_tools` and each tool successfully, proving the transport route rather than assuming it
- [ ] 6.3 Implement `health.py: check() -> {status: "ok"|"degraded", neo4j: "ok"|"unavailable"}` calling `driver.verify_connectivity()` and readiness logic that tolls tool calls with `graph_unavailable` when not ready, and verify integration test with mocked `verify_connectivity` success vs failure returns `ok` vs `degraded` and tool calls in degraded state return structured `graph_unavailable` with `isError: true`
- [ ] 6.4 Configure graceful shutdown — on SIGTERM stop accepting new tool calls, await in-flight calls (timeout 5s), then close driver, and verify no leaked sessions (subsequent query after close raises domain error, session context managers exercised)

## 7. Security hardening and error isolation

- [ ] 7.1 Enforce parameterized Cypher only — assert all graph queries use `$param` placeholders, no f-string Cypher, no tool-argument interpolation into query strings, and verify unit test injects `"' OR 1=1 //, ; MATCH (n) DETACH DELETE n; //, ../../secret` into `file_path` and asserts either validation rejects or query stores value literally via parameter without injection or deletion, plus no tool accepts `query`/`cypher` raw field (unknown field → `validation_error`)
- [ ] 7.2 Verify structured error envelope across all five tools — `status` in `ok|insufficient_history|not_found|validation_error|graph_unavailable|internal_error`, `error.code`/`message` actionable, no secrets in messages, and verify unit test forcing `neo4j.exceptions.ServiceUnavailable` maps to `GRAPH_UNAVAILABLE` with retry hint
- [ ] 7.3 Verify failure isolation — one tool's validation or graph error does not crash the server: call one tool with invalid `file_path` expecting `validation_error`, then call a second valid tool concurrently and verify second succeeds, and verify concurrent `asyncio.gather(10 tools)` all complete with correct per-call results
- [ ] 7.4 Verify bounded-limit enforcement — fuzz tests send `limit` as `100000`, `Infinity`, `-5`, `0`, `"20"` and oversized 4096-char `file_path` to all tools, asserting validation errors and no unbounded `MATCH` execution or server crash

## 8. Determinism, concurrency, and transport integration tests

- [ ] 8.1 Add determinism test — same tool with same args called twice on unchanged fixture graph returns byte-identical response (score, ranking, evidence ordering) and verify `pytest` passes
- [ ] 8.2 Add concurrency integration test — 10 concurrent `call_tool` invocations (mixed tools) via `asyncio.gather` against a live server with fixture graph all succeed with no cross-request contamination or session leaks, and verify `pytest -k mcp_concurrency` passes against disposable Neo4j
- [ ] 8.3 Add transport connectivity integration test — start MCP server on ephemeral port, connect with a standalone `mcp` client (separate process or `ClientSession`), assert `list_tools` → 5 tools, `call_tool` for each tool succeeds, and verify `pytest -k mcp_transport` passes
- [ ] 8.4 Add database-failure injection test — stub graph layer to raise `GraphNotAvailable`, call each tool and verify each returns `graph_unavailable` with `isError: true` and server remains running for next valid call

## 9. Documentation, validation, and housekeeping

- [ ] 9.1 Document MCP server usage in `docs/mcp.md` or `README` section — endpoint constant, tool contracts, validation rules, bounded limits, health vs readiness distinction, container-to-container hostname note for Compose, and verify docs match `specs/mcp/spec.md` claims
- [ ] 9.2 Ensure `src/chronotrace/mcp_server/` modules are included in the package (package-data) and `pyproject.toml` `mcp` dependency is declared so `pip install -e .` includes the transport
- [ ] 9.3 Run `openspec validate --strict` for this change and `pytest` for MCP-related unit/integration tests and verify both pass with no strict violations (requirement descriptions <=500 chars, every scenario has WHEN/THEN, Purpose >=50 chars, task checkboxes use `x` vs empty convention)

## 10. Completion gate

- [ ] 10.1 Demonstrate the iteration completion gate: a standalone MCP client (e.g., `mcp` Inspector or a throwaway `ClientSession` script) connects to the Streamable HTTP server and successfully calls all five tools (`get_change_criticality`, `get_regression_risk`, `get_historical_impact`, `recommend_tests`, `get_file_history`) against the demo graph populated by Iterations 2–5, returning evidence and limitations for each, and verify the demo script exits with status 0
