# Proposal: Iteration 6 — MCP Server and Five Tool Contracts

## Why

ChronoTrace has deterministic analytics (Iteration 5) that compute change criticality, regression risk, historical impact, and test recommendations directly from the Neo4j graph, but no narrow, validated interface exists to expose those capabilities to an agent or external client. Without a Model Context Protocol (MCP) server that wraps the analytics layer with strict input validation, bounded result limits, and a verified Streamable HTTP transport, the agent (Iteration 7) and API (Iteration 8) would need to duplicate analytics calls or bypass validation, violating the dependency direction (MCP tools call analytics, not the reverse) and the security constraint that arbitrary Cypher must never be exposed to the model.

Iteration 6 delivers the MCP server as the sole validated gateway between the graph/analytics layers and downstream consumers, exposing exactly five tool contracts with deterministic, evidence-backed outputs that the LLM can cite without inventing scores or relationships.

## What Changes

- **MCP server scaffolding** — Create `src/chronotrace/mcp_server/` (or `src/chronotrace/core/mcp/` per `PLAN.md` §4 layout) with `server.py` and `tools/` package. Initialize the shared graph driver and analytics services via validated configuration (Iteration 1) and Iteration 2/5 dependencies. Use the Python MCP SDK (`mcp` package) as the transport/server framework. No arbitrary Cypher execution; MCP tools call named analytics operations only.
- **Tool 1: `get_change_criticality`** — Inputs: `repository_id` (string, stable ID from Iteration 3) and `file_path` (repository-relative POSIX path). Output: structured result with `status` (`ok` | `insufficient_history` | `not_found` | failure), `repository_id`, `target`, `score`, per-component `breakdown` (decayed churn, fix involvement, co-change degree, import fan-in), `evidence` (source commits, relationship counts, component values), and `limitations`/`data_coverage` (history depth, excluded paths, parser diagnostics). Delegates to `analytics.criticality`.
- **Tool 2: `get_regression_risk`** — Inputs: `repository_id` + `file_path`. Output: historical fix commits that modified the target, other files changed in those fix commits ranked by co-occurrence count and directional confidence, with per-commit `hash`, `date`, `message`, `fix_reason` and per-file `confidence`. Framed as historical regression evidence, not a future-bug probability. Delegates to `analytics.regression`.
- **Tool 3: `get_historical_impact`** — Inputs: `repository_id` + `file_path`. Output: `importers`, `dependencies`, `co_changed` file lists, plus `potential_hidden_couplings` (co-change pairs with no direct `IMPORTS` edge, flagged as observed association not proven dependency). Each entry carries `relationship_type` and supporting commit evidence with counts. Delegates to `analytics.impact`.
- **Tool 4: `recommend_tests`** — Inputs: `repository_id` + `file_path`. Output: ranked candidate test files with per-candidate `score`, `rank`, and `reasons` drawn from four signals: test naming conventions, static import of target, historical co-change, and tests touched in related fix commits. No claim of real coverage. Delegates to `analytics.test_reco`.
- **Tool 5: `get_file_history`** — Inputs: `repository_id` + `file_path` + optional bounded `limit` (integer, see validation). Output: relevant file changes and commit evidence for the target file (`commits` with `hash`, `date`, `message`, `author`, `is_fix`, `fix_reason`, `additions`/`deletions`/`change_type`, and paginated `limit`/`total_count`), plus `evidence` and `limitations`. Analytics-backed with direct graph-layer commit history query for the file.
- **Shared validation and path safety** — For all five tools:
  - Validate `repository_id` is a non-empty string matching the stable-ID pattern (no injection; no path semantics).
  - Validate `file_path` is a repository-relative POSIX path: reject absolute paths, `..` traversal, empty strings, null bytes, and OS-absolute forms. Normalize with the canonical POSIX rules shared with ingestion/static analysis (Iteration 3/4).
  - Bound all result limits: `get_file_history` `limit` defaults to a documented value (e.g., 20 or 50), caps at a documented maximum (e.g., 100 or 200), rejects non-positive / non-integer / NaN values with a structured validation error. Other tools apply bounded defaults for ranked lists (e.g., top-K co-changed files, top-K test candidates) with documented maxima.
  - Map known domain failures (`not_found`, `insufficient_history`, `validation_error`, `graph_unavailable`) to structured tool errors that do not crash the server process and do not leak secrets, driver internals, or raw Cypher.
  - Return every successful tool output in the common result contract shape from Iteration 5: `status`, `repository_id`, `target`, `result`/`evidence`/`limitations` so the agent can ground claims.
- **Streamable HTTP transport** — Configure the MCP server's Streamable HTTP transport (MCP spec `Streamable HTTP`, successor to SSE) for consumption by the Agent API (Iteration 7) via an MCP client. Verify the actual transport route and SDK behaviour (endpoint path, e.g., `/mcp` or SDK-default, headers, `Accept: text/event-stream` vs `application/json` negotiation) by integration test rather than assumed URL. Bind host/port from validated config or environment; container-to-container addressing uses the Compose service hostname (not `localhost`) when run under Docker (Iteration 9).
- **Health/readiness behaviour** — Expose a health/readiness check (separate from MCP tool list) that verifies Neo4j connectivity via `graph.verify_connectivity()` and analytics availability. Tool invocations when dependencies are unavailable return a structured `graph_unavailable` / `service_not_ready` error rather than a crash. Do not report the MCP server as ready if Neo4j is unreachable (mirrors Iteration 2 readiness gate).
- **Concurrency and failure isolation** — Server handles concurrent tool calls safely (driver sessions are short-lived per call, not shared; no global mutable state per request). Tool failures (validation, graph error, analytics error) are isolated: one tool's exception does not crash the server or affect other in-flight calls. Tests cover concurrent calls and database-failure injection.
- **One-tool-first delivery** — Implement one tool (e.g., `get_change_criticality`) end-to-end first, test it independently, then add the remaining four tools reusing the validation/transport/error-mapping scaffolding.

## Capabilities

### New Capabilities

- `mcp`: MCP server and five validated tool contracts — server lifecycle (Streamable HTTP transport, health/readiness, concurrent connection handling), shared validation and path-safety rules (repository_id/file_path/limit checks, path-traversal prevention, normalization, bounded limits), structured result/error envelope (`status`, `repository_id`, `target`, `result`, `evidence`, `limitations`), and the five tool behaviours (`get_change_criticality`, `get_regression_risk`, `get_historical_impact`, `recommend_tests`, `get_file_history`) that delegate to deterministic analytics and graph queries via parameterized Cypher only. Owns the MCP SDK integration, transport route verification, and failure isolation consumed by the agent (Iteration 7) and indirectly by the API (Iteration 8).

### Modified Capabilities

- None — no existing shipped spec capability is modified. Iterations 0–5 established foundation, config, graph, ingestion, static analysis, and analytics; this iteration is additive and read-only over the graph. The `analytics` capability (Iteration 5) is consumed (not changed) via its public Python API; `graph` (Iteration 2) is read via existing parameterized queries.

## Impact

- **New code**: `src/chronotrace/mcp_server/` — `server.py` (MCP server factory, Streamable HTTP transport wiring, lifespan), `tools/__init__.py`, `tools/validation.py` (path/repository_id/limit validators, canonical POSIX normalization), `tools/criticality.py`, `tools/regression.py`, `tools/impact.py`, `tools/test_reco.py`, `tools/file_history.py` (each tool handler mapping validated inputs → analytics/graph call → structured output), `tools/errors.py` (validation → tool error mapping), optional `health.py`.
- **Graph layer**: New read-only queries for `get_file_history` (commit history for a single file, bounded/paginated) added as parameterized Cypher in `src/chronotrace/core/graph/repositories/` or `src/chronotrace/graph/` per Iteration 2 layout; reuses driver lifecycle, session hygiene, repository isolation, and `MERGE`/`MATCH` conventions. No writes, no schema migration required (no new node/relationship types; file history is read from existing `Commit -[:MODIFIED]-> File` and `Commit` metadata).
- **Analytics layer**: Consumed as library calls (`analytics.service` or per-capability modules from Iteration 5); no changes to scoring formulae, decay, or result contracts. The `mcp` tools are thin validated wrappers that preserve analytics' deterministic outputs and `status`/`evidence`/`limitations` shapes.
- **Configuration**: Reads `graph.uri`/`database`/`batch_size`, `analysis.history.decay_half_life_days`, `mcp.host`/`port` (new optional keys under validated `chronotrace.yml` or environment, with Pydantic validation, defaults, and clear errors for unknown keys). Secrets (`NEO4J_USERNAME`, `NEO4J_PASSWORD`, `MODEL_API_KEY`) remain environment-only; no secrets in YAML and no secret leakage in tool errors or logs.
- **Dependencies**: New runtime dependency `mcp` (Python MCP SDK) added to `pyproject.toml`; no other new external packages. Python 3.12+, stdlib otherwise. Docker/Compose wiring deferred to Iteration 9 (this iteration ships `mcp-server` as a runnable module with CLI entry; Compose service `mcp-server` in `docker-compose.yml` reuses the backend image with a different startup command in Iteration 9).
- **Downstream**: Unblocks Iteration 7 (agent — MCP client consumes Streamable HTTP, supplies all five tool definitions to Muse Spark, executes tool-calling loop) and Iteration 8 (API — agent and report routes can operate via MCP when model is available, deterministic report path remains available when not). UI (Iteration 11) and backtesting (Iteration 12) indirectly benefit via agent/API evidence quality.
- **Security surface**: Validation boundary widened to MCP tool inputs (repository_id, file_path, limit). Mitigations: shared validators tested against path-traversal payloads, bounded limits, parameterized Cypher only, no model-generated Cypher, no tool-argument pass-through to `session.run()` without validation. Tool outputs treated as data, not instructions, by the future agent.

## Out of Scope (Explicitly Excluded)

- Model client, tool-calling loop, and evidence-first prompting (Iteration 7).
- FastAPI application, lifespan, health/readiness endpoints, chat/report routes, background ingestion jobs (Iteration 8).
- Docker images, multi-service Compose orchestration, service hostnames, and volume persistence (Iteration 9).
- CLI commands `add`/`chat`/`report`/`up`/`down`/`status` (Iteration 10) and web UI (Iteration 11).
- SZZ, function/symbol-level tracking, real per-test coverage, embeddings, multi-language parsing, backtesting evaluation (PLAN §7 exclusions, Iteration 12).
- New graph node/relationship types or schema migrations beyond the read-only file-history query.
- Automatic `MERGE` writes from MCP — server is strictly read-only over the graph.

## Acceptance Criteria (Iteration-level)

Maps to PLAN.md Iteration 6 — Scope, Security constraints, and Completion gate:

- Each of the five tools can be invoked independently via a standalone MCP client and returns the documented structured output (`status`, `repository_id`, `target`, result fields, `evidence`, `limitations`).
- Inputs are validated and bounded: empty/traversal/absolute paths, unknown `repository_id`, malformed `limit` (non-integer, negative, zero, NaN, unbounded) return structured validation errors; unknown repository/file cases return `not_found` rather than crashes or false data.
- Tool outputs include traceable evidence (commit hashes, dates, messages, fix reasons, directional confidence, relationship counts) and limitations (shallow history, excluded paths, parser failures where relevant) matching Iteration 5 contracts; no zero-risk claim on missing evidence.
- MCP Streamable HTTP transport can be reached by a separate process/client (verified route, not assumed URL); health/readiness reflects graph connectivity (unavailable → structured error, not ready).
- Tool failures (validation, graph unavailable, analytics error) do not crash the server process; concurrent tool calls are safe and tested.
- Tests cover: per-tool contract validation (happy + error paths), path-traversal and bounded-limit edge cases, transport connectivity (client connects and calls tools), concurrency, and database-failure injection. No arbitrary Cypher exposure, no model-generated query string acceptance.
- Completion gate: a standalone MCP client can connect to the server and call all five tools against the demo graph populated by Iterations 2–5.

## Dependencies and Sequencing

- **Depends on**: Iteration 1 (validated `chronotrace.yml` schema, secrets, path validation, configuration precedence), Iteration 2 (Neo4j driver lifecycle, repository isolation, parameterized Cypher, session/transaction hygiene), Iteration 3 (stable `repository_id`, file identity), Iteration 4 (`IMPORTS` graph for impact), Iteration 5 (four analytics operations, common result contract, decay, evidence shapes).
- **Unblocks**: Iteration 7 (agent — MCP client, tool definitions, tool loop, iteration limits), Iteration 8 (API — chat/report routes invoking MCP/agent), Iteration 9 (Compose `mcp-server` service), Iteration 12 (integration/hardening including MCP + agent E2E).
