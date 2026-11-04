# Design: Iteration 6 — MCP Server and Five Tool Contracts

## Context

After Iterations 2–5 ChronoTrace has a versioned Neo4j graph (Iteration 2), a historical ingestion pipeline (Iteration 3), a current import graph built via AST without execution (Iteration 4), and deterministic analytics (Iteration 5) exposing four operations plus a common result contract (`status`, `repository_id`, `target`, `result`, `evidence`, `limitations`) with exponential time-decay. No MCP interface exists yet; the agent (Iteration 7) and API (Iteration 8) have nothing validated to call, and allowing the LLM to compute scores or issue Cypher would violate PLAN §2.2 non-negotiables.

Iteration 6 introduces `src/chronotrace/mcp_server/` as the sole validated MCP gateway. It reuses the Iteration 1 validated config object (never re-parsing YAML/env inside the server), the Iteration 2 driver lifecycle (single application-level driver, short-lived sessions, parameterized Cypher), and the Iteration 5 analytics Python API. The Python MCP SDK (`mcp`) provides the server and Streamable HTTP transport. The server is strictly read-only over the graph; all Cypher is parameterized and confined to the graph layer.

See `proposal.md` — Why/What Changes for motivation and `specs/mcp/spec.md` for normative tool contracts; this doc covers *how*.

Constraints:
- Python 3.12+, `src` layout, Typer/Rich/FastAPI/Neo4j already in `pyproject.toml`.
- Secrets from environment only; no YAML secrets; error messages must not leak secrets or raw Cypher.
- Repository isolation: every read filtered by `repository_id` (stable ID from Iteration 3, repository-scoped).
- Path safety: canonical POSIX-relative paths shared with ingestion/static analysis; path traversal must be prevented.
- Bounded limits on every tool; no unbounded result sets.
- No arbitrary Cypher exposure; MCP tools call named analytics operations only.

## Goals / Non-Goals

**Goals:**
- Ship a runnable MCP server with Streamable HTTP transport that a separate client process can connect to and that cleanly starts/stops (lifespan, driver close) without leaking sessions.
- Expose exactly five validated, bounded tools (`get_change_criticality`, `get_regression_risk`, `get_historical_impact`, `recommend_tests`, `get_file_history`) that validate inputs, normalize paths, delegate to analytics/graph, and return the common result envelope with evidence and limitations.
- Provide shared validation (repository_id, file_path, limit), path-traversal prevention, normalization, and structured domain-error mapping that is reused across all tools and independently testable without a live Neo4j.
- Verify the real transport route and SDK behaviour via integration tests rather than assuming a URL.
- Keep the server concurrent-safe and failure-isolated: one tool's validation/graph failure does not crash the server or other in-flight calls.
- Leave the stack ready for Iteration 7 (MCP client in the agent) and Iteration 9 (Compose `mcp-server` service reusing the backend image).

**Non-Goals:**
- Model provider client, Muse Spark integration, tool-calling loop, reasoning controls, or evidence-first prompting (Iteration 7).
- FastAPI app, request schemas, health/readiness HTTP endpoints, ingestion background jobs, report rendering (Iteration 8) — the MCP server's health/readiness is a minimal check consumed later by the API/Compose layer.
- Docker image build and multi-service Compose orchestration (Iteration 9); filesystem/secret plumbing beyond reading the validated config object.
- CLI commands and web UI (Iterations 10–11).
- Backtesting, SZZ, function-level tracking, or any analytics formula change (Iteration 12 / PLAN §7 exclusions).

## Decisions

### Decision 1: Python MCP SDK (`mcp`) with `FastMCP`/`Server` and Streamable HTTP transport

**Choice:** Use the official `mcp` Python SDK. Define the server via `FastMCP` (if available in the pinned SDK) or `mcp.server.Server` with `@server.tool()` / `@mcp.tool()` decorators. Expose Streamable HTTP via `mcp.server.sse` predecessor replaced by `streamable_http` — in practice `mcp run --transport streamable-http` or programmatically `server.serve(host, port, transport="streamable-http")`. Verify against the installed SDK's actual module path (e.g., `mcp.server.streamable_http` or `mcp.server.sse` fallback) and pin the SDK version in `pyproject.toml`.

**Rationale:** This is the MCP-spec-compliant path; the spec's Streamable HTTP is the current successor to SSE and is what the model SDK and MCP Inspector expect. Using the SDK's own transport avoids hand-rolling HTTP/SSE and keeps session management, JSON-RPC framing, and error envelopes correct.

**Alternatives considered:**
- Raw FastAPI route that mimics MCP JSON-RPC — rejected: fragile, diverges from spec, Inspector incompatible, duplicates SDK work.
- Stdio transport only — rejected: Iteration 6 explicitly requires Streamable HTTP for Agent API consumption; stdio is CLI-local only.

### Decision 2: Module layout `src/chronotrace/mcp_server/` with `tools/` subpackage

**Choice:**
```
src/chronotrace/mcp_server/
  __init__.py
  server.py        # create_mcp_server(config, graph_gateway, analytics_service) -> FastMCP/Server, run(), lifespan
  tools/
    __init__.py
    validation.py  # validate_repository_id, validate_file_path, validate_limit, normalize_path
    errors.py      # ToolValidationError, map_domain_error -> MCP error envelope
    criticality.py # handler for get_change_criticality
    regression.py  # handler for get_regression_risk
    impact.py      # handler for get_historical_impact
    test_reco.py   # handler for recommend_tests  (tool name recommend_tests)
    file_history.py# handler for get_file_history (+ graph file-history repository)
  health.py        # optional: check() -> GraphConnectivity + analytics readiness
```

**Rationale:** Keeps per-tool handlers small and independently testable; shared validation/errors are importable by unit tests without spinning a server. Aligns with PLAN §4 (`src/chronotrace/mcp_server/server.py`, `mcp_server/tools/`) and with Iteration 2/3/4 layering.

**Alternatives considered:**
- One monolithic `server.py` with all tools — rejected: hard to test validation in isolation, merges unrelated error paths.

### Decision 3: Strict input validation before any graph/analytics call (fail-fast)

**Choice:** Each tool handler calls shared validators first; only on success does it invoke analytics/graph.

Validation rules:
- `repository_id`: required, `str`, non-empty after strip, length bounded (e.g., 1–256), characters matching stable-ID alphabet (alphanumerics, `-_:/.` and the canonical repo-ID hash pattern); reject `None`, numbers, objects. No path semantics applied to this field.
- `file_path`: required, `str`, non-empty, POSIX-relative after normalization. Normalization chain: `strip()` → reject `""` → `Path(p).as_posix()` → reject if `Path(p).is_absolute()` (POSIX absolute or Windows drive `C:`/`\\`) → split on `/` and reject any segment equal to `..` or containing `\x00` → reject `.` or `//` double-slash collapsed by `Path` still flagged if original contained `..` before normalization → normalized output is `posixpath` with no leading `/` and no `..` segments. On Windows the input `a\b\c.py` is treated as traversal attempt if it contains `\` that would become `/` after `as_posix` without explicit allowance — validators reject backslash-containing paths and require caller to send POSIX form (canonical form). This prevents canonicalization bypass.
- `limit` (only `get_file_history`): optional, type `int` (or JSON number that maps to int), default e.g. `20`, range `1 <= limit <= 100` (exact maxima documented in spec and validated here). Reject `0`, negatives, floats that are not integral, `NaN`, `Infinity`, strings, missing `limit` uses default. Other tools' internal top-K bounds (e.g., `recommend_tests` returns at most 20 candidates, `get_regression_risk` caps co-changed files) are server-side constants, not client-supplied.
- Unknown extra tool arguments: rejected with structured `validation_error` (strict mode), or ignored with documented behaviour — default to reject to catch model-generated typos.

Error shape on validation failure: return a JSON object with `status: "validation_error" | "not_found" | "insufficient_history" | "graph_unavailable"` plus `error: { code, message }` (message actionable, no secrets). Validation errors are returned as MCP tool result with `isError: true` per MCP spec rather than throwing an unhandled exception that would tear down the stream.

**Rationale:** Validates the "parse, don't validate" boundary so graph/analytics never sees untrusted strings; prevents path-traversal reads that could leak cross-repo data and Cypher injection via crafted paths. Shared validators give one place to add bypass tests (e.g., `../../etc/passwd`, `C:\Windows\file`, `%2e%2e%2f` already decoded by JSON).

**Alternatives considered:**
- Per-tool inline validation — rejected: divergence risk, missed traversal variant.
- Delegating validation to graph `WHERE canonical_path = $path` — rejected: defence-in-depth requires rejection before DB round-trip.

### Decision 4: Delegation to Iteration 5 analytics (thin wrappers, no formula duplication)

**Choice:** Tool handlers are thin adapters:

```python
async def get_change_criticality(repository_id, file_path):
    repo_id = validate_repository_id(repository_id)
    path = validate_file_path(file_path)  # normalized POSIX
    result = analytics_service.get_change_criticality(
        repo_id, path
    )  # pure Python, raises analytics domain errors
    return to_tool_envelope(
        result
    )  # status/repository_id/target/score/breakdown/evidence/limitations
```

For `get_file_history`, which analytics does not already cover, add a focused read-only repository `FileHistoryRepository.get_commits_for_file(repository_id, canonical_path, limit, offset?)` that runs a single parameterized Cypher `MATCH (c:Commit)-[r:MODIFIED]->(f:File {repository_id:$repo, canonical_path:$path}) RETURN c, r ORDER BY c.date DESC LIMIT $limit` (with `total_count` via `COUNT` or second query). No formula or scoring logic is duplicated here.

**Rationale:** Enforces dependency direction (MCP → analytics → graph) and avoids the "MCP calculates its own scores" violation (PLAN §3.2). Keeps numeric logic in one place so backtesting (Iteration 12) only targets analytics.

**Alternatives considered:**
- MCP tools issue direct Cypher with custom WHERE clauses — rejected: bypasses analytics contracts and risks arbitrary-Cypher exposure.

### Decision 5: Transport endpoint verification, not assumption

**Choice:** The server's Streamable HTTP endpoint path is whatever the SDK declares (commonly `/mcp` for `streamable-http` or `/sse` legacy). The codebase defines `MCP_HTTP_PATH` as a constant imported from the SDK or explicitly set in `server.py` and the integration test imports that constant when constructing the client URL. Tests spin a server on an ephemeral port (`port=0` or `get_free_port()`) and perform: list tools → assert five tools present → call each tool → assert response shape. No hard-coded assumption URL is documented for users until the test proves it.

**Rationale:** The MCP SDK has renamed the Streamable HTTP route across versions; hard-coding a URL in docs/tests that differs from the SDK causes "transport unreachable" failures in review. A constant + test closes the loop.

**Alternatives considered:**
- Documenting a fixed `/mcp` URL without test — rejected: unverified.

### Decision 6: Lifespan-managed shared driver and graceful shutdown

**Choice:** `create_mcp_server(config)` builds a single `GraphGateway`/`Neo4jDriver` (Iteration 2) and `AnalyticsService(graph)` once, injects them into tool closures, and exposes `async lifespan` / `asynccontextmanager` or `try/finally` that calls `driver.close()` on shutdown. Each tool call does `with driver.session(database=config.graph.database) as s: ...` (short-lived), never holding a session across await points. Server startup runs `health.check()` (driver `verify_connectivity`) before declaring readiness; if unreachable, the MCP server still starts but each tool returns `graph_unavailable` until connectivity recovers (alternative: refuse to start — chosen to keep process debuggable under Compose restart loops; Iteration 9 may gate startup on readiness probe).

**Rationale:** Matches Iteration 2's "one driver, many short-lived sessions" contract and avoids socket exhaustion under concurrent tool calls.

**Alternatives considered:**
- Creating a driver per tool call — rejected: per-call driver creation violates Iteration 2 scope and leaks pools.
- Refusing to start when Neo4j is down — noted as alternative; current choice keeps the server observable while failing tools cleanly.

### Decision 7: Structured error envelope (MCP-compatible, no crashes)

**Choice:** All tool handlers wrap the analytics/graph call in:

```python
try:
    result = analytics.call(...)
except ValidationError as e:
    return {
        "status": "validation_error",
        "error": {"code": e.code, "message": e.message},
        "isError": True,
    }
except GraphNotAvailable | GraphConnectionError:
    return {
        "status": "graph_unavailable",
        "error": {
            "code": "GRAPH_UNAVAILABLE",
            "message": "Graph is temporarily unavailable; retry",
        },
    }
except FileNotFound | RepositoryNotFound:
    return {"status": "not_found", "error": ...}
except Exception as e:
    log.exception(e)  # no secrets in log
    return {"status": "internal_error", "error": {"code": "INTERNAL", "message": "Internal error"}}
```

No handler re-raises a non-MCP exception that would abort the stream. `status` values align with Iteration 5 (`ok`, `insufficient_history`, `not_found`, failure variants) plus `validation_error` / `graph_unavailable` for transport-level concerns. MCP `isError` flag is set per spec for error results; success results have `isError: false` or omitted per SDK default.

**Rationale:** Prevents one bad tool argument or transient DB outage from killing the Agent's long-lived MCP connection (required for the tool loop in Iteration 7).

### Decision 8: Bounded result limits and pagination

**Choice:**
- `get_file_history`: `limit` client-supplied, default 20, max 100. Server enforces cap; if client sends `limit=1000`, returns `validation_error` or clamps with a documented response field (`limit_applied: 100, requested: 1000`) — default to validation error to keep the contract strict and testable.
- Other tools: server-side caps (e.g., `get_regression_risk` returns at most 20 co-changed files ranked; `recommend_tests` at most 20 candidates; `get_historical_impact` each list capped at 50) as constants in `tools/*`. These caps are not client-tunable in this iteration (no `limit` argument beyond `get_file_history`), keeping the surface small.
- No `offset`/`cursor` pagination in this iteration beyond `get_file_history` `limit`; future pagination can add `offset` without breaking.

**Rationale:** Prevents a model-generated `limit=100000` from forcing a huge `MATCH`/`RETURN` and OOM, and satisfies PLAN's "bound all result limits" requirement.

### Decision 9: Security hardening (no arbitrary Cypher, injection tests)

**Choice:** All graph reads go through the Iteration 2 repositories with fixed Cypher templates containing only `$param` placeholders. Tool arguments are never interpolated into Cypher. Linters/tests inject payloads like `"' OR 1=1 //`, `"; MATCH (n) DETACH DELETE n; //`, and `../../secret` into `file_path` and assert they are either rejected by validation or stored/queried literally via parameter (no injection, no deletion). The server has no method that accepts a `query` or `cypher` argument.

**Rationale:** Satisfies the non-negotiable "Never expose arbitrary Cypher execution to the agent" (PLAN §2.2) at the code boundary, not just in documentation.

### Decision 10: Health/readiness distinction and non-blocking declaration

**Choice:** Define `health.check()` → `{"status": "ok"|"degraded", "neo4j": "ok"|"unavailable", "analytics": "ok"}`. The MCP transport's HTTP health endpoint (if exposed by the SDK, otherwise a separate `/health` FastAPI shim in Iteration 8) reports `health` (process is up) vs `readiness` (graph is reachable). The MCP server itself is "healthy" when the process is running; it is "ready" for tool calls when `verify_connectivity()` succeeds. Tool calls when not ready return `graph_unavailable`, not an HTTP 500.

**Rationale:** Prevents the API/Compose readiness probe from marking the service ready before Neo4j accepts queries, which would hide ingestion/analytics failures behind MCP timeouts.

## Risks / Trade-offs

- **MCP SDK Streamable HTTP path changes across SDK versions** → Mitigation: pin SDK version in `pyproject.toml`, centralize the path constant, and gate on the integration test that asserts client can connect; on upgrade, the test fails fast and prompts a path/docs update.
- **Concurrent tool calls contend on driver pool** → Mitigation: short-lived sessions, pool size from Neo4j driver defaults, and a concurrency integration test (`asyncio.gather` 10 tools in parallel) that asserts all succeed and no session leaks; document pool tuning as future knob if needed.
- **Path-traversal bypass via Unicode/percent-encoding/backslash** → Mitigation: validators reject `\x00`, backslashes, `..` segments before and after `as_posix`, and operate on the raw JSON string (already percent-decoded by the HTTP stack). Bypass tests include `..%2f`, `%2e%2e/`, `..\`, `a/b\../c`, and `nul-byte%00.py`.
- **Model-generated tool arguments overflow limits** → Mitigation: strict `limit` validation and server-side caps; fuzz tests send large/negative/float `limit` values and oversized `file_path` (e.g., 4096-char path) and assert validation errors.
- **Neo4j unavailable at MCP startup** → Mitigation: server stays up but reports degraded readiness; tool calls return `graph_unavailable` with retry hint rather than HTTP 500 or process exit; Compose healthcheck in Iteration 9 can restart on prolonged degraded state.
- **Analytics API evolves, MCP tools drift from analytics contracts** → Mitigation: MCP handlers are tested against the same fixture graph as analytics unit tests; a change to analytics result shape breaks MCP unit tests, enforcing contract alignment. MCP does not copy formula logic — only shapes evidence.
- **`get_file_history` count query cost on large histories** → Mitigation: `LIMIT` without full scan is bounded; `total_count` can be derived via `COUNT` with indexed `File(canonical_path, repository_id)` and `Commit(date)`; if count query proves slow on repos with >50k commits, omit `total_count` and return `has_more` boolean instead (spec allows either; document choice after profiling).
- **Error messages leak path existence** → Mitigation: `not_found` for missing repository vs missing file is distinguished (agent needs to know which is missing) but messages do not confirm arbitrary filesystem state; repository existence is checked via `Repository` node existence, not filesystem access.
- **Server shutdown races with in-flight tools** → Mitigation: lifespan/close drains: on SIGTERM, stop accepting new tool calls, await in-flight calls to finish (with timeout e.g., 5s), then `driver.close()`.

## Migration Plan

No schema or data migration required in this iteration — the server is read-only.

1. Add `mcp>=X.Y` to `pyproject.toml` runtime dependencies; `pip install -e ".[dev]"` pulls the SDK and makes `chronotrace mcp` / `python -m chronotrace.mcp_server.server` runnable.
2. Land `src/chronotrace/mcp_server/` without changing existing CLI/API behaviour (no callers yet); existing tests for config/graph/ingestion/analytics remain green.
3. Run a local Neo4j (`docker compose up neo4j` — existing compose) with a fixture graph populated by Iterations 2–5 test fixtures; start the MCP server on an ephemeral port and verify via a throwaway client that `list_tools` returns five tools and each tool returns the expected envelope on the fixture (the iteration completion gate).
4. No persistent-state change to rollback: stop the server and `driver.close()` releases pooled connections; restarting with the same config is idempotent. If the SDK pin needs downgrade, `pip install mcp==prev --force-reinstall` suffices; no graph data is touched.
5. Forward compatibility: Iteration 7 will add an `McpClient` in `src/chronotrace/agent/mcp_client.py` that connects to this server's Streamable HTTP URL; Iteration 9 will add `mcp-server` to `docker-compose.yml` reusing the backend image with `command: ["python","-m","chronotrace.mcp_server.server","--host","0.0.0.0","--port","8001"]` and internal hostname `mcp-server`.

## Open Questions

- **MCP server health endpoint placement:** Should health be a native MCP tool (`check_health`) or a plain HTTP `/health` route alongside the Streamable HTTP endpoint? The SDK's streamable transport may already expose a health resource — defer the exact shape until manual testing against the installed SDK; the spec requires health behaviour but not the URL, and tasks verify the SDK's real route.
- **Shared port vs. dedicated port for MCP:** Run the MCP server in the same process as the FastAPI `agent-api` (Iteration 8) or as a dedicated process on its own port? Iteration 6 scope mandates a separate `mcp-server` process on its own port (Streamable HTTP transport) per PLAN §3.3's three-container strategy; lifecycle sharing is deferred to Compose wiring in Iteration 9.
- **Pagination for `get_file_history` beyond `limit`:** Should `offset` or cursor-based pagination be added now? Deferred — cap at `limit` with `total_count`/`has_more` suffices for the agent's typical "recent history" queries; adding `offset` later is a non-breaking additive change.
