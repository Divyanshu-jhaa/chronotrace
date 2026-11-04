# mcp Specification

## Purpose

Exposes the deterministic analytics engine through a validated, bounded MCP server with five tool contracts and a Streamable HTTP transport that downstream agent and API layers can consume without duplicating scoring logic or accepting arbitrary queries.

## Requirements

### Requirement: MCP server lifecycle and Streamable HTTP transport

The system SHALL provide an MCP server that initializes the shared graph and analytics dependencies, exposes the five tools via the Python MCP SDK, and serves a Streamable HTTP transport on a configurable host and port whose real route is verified by integration test rather than assumed.

#### Scenario: Server exposes five tools

- **WHEN** a standalone MCP client connects and lists tools
- **THEN** the server returns exactly `get_change_criticality`, `get_regression_risk`, `get_historical_impact`, `recommend_tests`, and `get_file_history` with their documented input schemas

#### Scenario: Streamable HTTP transport is reachable

- **WHEN** the server is started with Streamable HTTP transport
- **THEN** a separate client process can connect to the verified transport endpoint (e.g., `http://host:port/mcp` or the SDK's declared path) and successfully list and call tools; the route is not a hard-coded guess but is imported from a tested constant

#### Scenario: Server reuse of shared dependencies

- **WHEN** the server starts
- **THEN** it reuses a single application-level Neo4j driver and analytics service instance for all tool calls rather than creating per-call drivers or duplicating analytics logic

#### Scenario: Graceful shutdown closes driver

- **WHEN** the server is shut down (SIGTERM or lifespan close)
- **THEN** it drains in-flight tool calls, closes the shared driver, and subsequent calls raise a clear not-available error without leaking sessions

### Requirement: Shared input validation for repository_id

The system SHALL validate `repository_id` on every tool call before any graph or analytics access, rejecting missing, empty, non-string, or malformed values with a structured validation error.

#### Scenario: Valid repository_id accepted

- **WHEN** a tool is called with a non-empty string repository identifier matching the stable-ID pattern
- **THEN** validation passes and the handler proceeds to normalize the file path and invoke the backing operation

#### Scenario: Empty repository_id rejected

- **WHEN** a tool is called with `""`, whitespace-only, `null`, or a non-string value for `repository_id`
- **THEN** the tool returns a structured `validation_error` with `isError: true` and does not query Neo4j

#### Scenario: Repository not found returns not_found

- **WHEN** a tool is called with a syntactically valid but unknown `repository_id` that has no `Repository` node in Neo4j
- **THEN** the tool returns `status: "not_found"` with an actionable message and does not expose stack traces or driver internals

### Requirement: Shared validation and normalization for file_path with path-traversal prevention

The system SHALL validate and canonicalize `file_path` on every tool call, accepting only repository-relative POSIX paths and rejecting absolute paths, parent-traversal segments, null bytes, and Windows absolute forms; it SHALL normalize with the ingestion/static-analysis POSIX canonical rules before querying the graph.

#### Scenario: Repository-relative POSIX path accepted

- **WHEN** a tool is called with `src/foo/bar.py` or `pkg/__init__.py`
- **THEN** the path is normalized to its POSIX canonical form and used as the validated target for the analytics/graph query

#### Scenario: Absolute path rejected

- **WHEN** a tool is called with `/etc/passwd`, `/a/b.py`, `C:\Windows\file.py`, or `\\server\share\file.py` as `file_path`
- **THEN** the tool returns `validation_error` with `isError: true` before any graph access

#### Scenario: Parent-traversal rejected

- **WHEN** a tool is called with `../secret.py`, `a/../b.py`, `a/b/../../c.py`, or an encoded variant that normalizes to a path containing `..` segments
- **THEN** validation rejects it with `validation_error` and no graph query is executed

#### Scenario: Backslash and null-byte rejected

- **WHEN** a tool is called with `a\b\c.py` containing backslashes or a path containing `\x00` null byte
- **THEN** validation rejects it with `validation_error`

#### Scenario: File not found returns structured not_found

- **WHEN** a tool is called with a syntactically valid but unknown `file_path` for an existing repository
- **THEN** the tool returns `status: "not_found"` with an actionable message identifying the missing file within the repository scope

### Requirement: Bounded limits for get_file_history

The system SHALL bound the optional `limit` argument of `get_file_history` with a documented default and maximum, validating type and range and returning a structured error for invalid values; other tools SHALL apply documented server-side caps that are not client-tunable in this iteration.

#### Scenario: Default limit applied when omitted

- **WHEN** `get_file_history` is called without `limit`
- **THEN** the server applies the documented default (e.g., 20) and returns at most that many commits ordered by date descending

#### Scenario: Valid limit respected up to maximum

- **WHEN** `get_file_history` is called with `limit: 50` within the allowed range (1 to max, e.g., 100)
- **THEN** the server returns at most 50 commits and includes `total_count` or `has_more` metadata when available

#### Scenario: Out-of-range limit rejected

- **WHEN** `get_file_history` is called with `limit: 0`, negative, float, `NaN`, string, or a value exceeding the documented maximum (e.g., 1000 > 100)
- **THEN** the server returns `validation_error` with `isError: true` and does not execute an unbounded graph query

#### Scenario: Server-side caps for other tools

- **WHEN** `get_regression_risk` or `recommend_tests` produce ranked lists
- **THEN** each list is capped at a documented server-side maximum (e.g., 20 co-changed files, 20 test candidates) even when more evidence exists, preventing unbounded payloads

### Requirement: get_change_criticality tool contract

The system SHALL expose a `get_change_criticality` MCP tool that accepts validated `repository_id` and `file_path`, delegates to the deterministic change-criticality analytics, and returns the common result envelope with `status`, `repository_id`, `target`, score, per-component breakdown, evidence, and limitations.

#### Scenario: Successful criticality for known file

- **WHEN** `get_change_criticality` is called with a valid repository and a file that has history and import data
- **THEN** the response has `status: "ok"`, the numeric `score`, a `breakdown` with decayed churn, fix involvement, co-change degree, and import fan-in components, plus `evidence` (source commits, counts) and `limitations` fields matching the analytics contract

#### Scenario: Insufficient history disclosed

- **WHEN** `get_change_criticality` is called for a file with insufficient commit history to score (e.g., single commit, shallow history, excluded paths)
- **THEN** the response has `status: "insufficient_history"` with an explicit `limitations` reason and does not present the missing evidence as zero risk

#### Scenario: Unknown file returns not_found

- **WHEN** `get_change_criticality` is called for a path that does not correspond to a `File` node in the repository
- **THEN** the tool returns `status: "not_found"` with an actionable message

### Requirement: get_regression_risk tool contract

The system SHALL expose a `get_regression_risk` MCP tool that accepts validated `repository_id` and `file_path` and returns historical fix commits that modified the target and the files changed alongside it, ranked by co-occurrence count and directional confidence, with per-commit evidence.

#### Scenario: Regression risk with fix history

- **WHEN** `get_regression_risk` is called for a file that has been modified in heuristic fix commits
- **THEN** the response contains `fix_commits` with `hash`, `date`, `message`, `fix_reason`, and `co_changed_files` ranked by `count` and `confidence` with deduplicated commits and no double-counting

#### Scenario: No fix evidence is explicit

- **WHEN** `get_regression_risk` is called for a file with no fix commits touching it
- **THEN** the response has `status: "ok"` or `insufficient_history` (per analytics contract) with an explicit message that no fix evidence was found, not a claim of zero future risk, and an empty or absent `co_changed_files` list

#### Scenario: Ranking is deterministic

- **WHEN** `get_regression_risk` is called twice on the same graph snapshot and configuration
- **THEN** the returned ranking order and confidence values are byte-identical

### Requirement: get_historical_impact tool contract

The system SHALL expose a `get_historical_impact` MCP tool that accepts validated `repository_id` and `file_path` and returns the file's importers, dependencies, co-changed files, and potential hidden couplings (co-change pairs with no direct import edge), each with relationship type and supporting commit evidence.

#### Scenario: Impact returns all four relationship groups

- **WHEN** `get_historical_impact` is called for a file that has importers, dependencies, and co-change relationships
- **THEN** the response contains `importers`, `dependencies`, `co_changed`, and `potential_hidden_couplings`, each entry with `path`, `relationship_type`, and supporting `commits`/`count` evidence

#### Scenario: Hidden couplings flagged as observed association

- **WHEN** a co-change pair has no direct `IMPORTS` relationship
- **THEN** that pair appears in `potential_hidden_couplings` and the response text/field documents that it is an observed association, not proof of a direct dependency

#### Scenario: No evidence does not imply no impact

- **WHEN** `get_historical_impact` is called for a file with no resolved import edges and no co-change history
- **THEN** the response explicitly states limitations (no static relationship resolved, insufficient co-change evidence) and does not claim the file has no impact

### Requirement: recommend_tests tool contract

The system SHALL expose a `recommend_tests` MCP tool that accepts validated `repository_id` and `file_path` and returns ranked candidate test files with per-candidate score, rank, and reasons derived from test naming conventions, static imports, historical co-change, and fix-commit co-occurrence.

#### Scenario: Test ranking returns scored candidates

- **WHEN** `recommend_tests` is called for a file that has related tests by naming, import, or co-change
- **THEN** the response contains `candidates` each with `path`, `score`, `rank`, and `reasons` (e.g., `name_match`, `imports_target`, `co_changed`, `touched_in_fix`), ordered by descending score

#### Scenario: No guarantee of coverage

- **WHEN** `recommend_tests` returns candidates
- **THEN** the response does not claim real coverage or guaranteed behavioural relevance and includes a `limitations` note to that effect

#### Scenario: File with no related tests handled explicitly

- **WHEN** `recommend_tests` is called for a file with no candidate tests matching any signal
- **THEN** the response has `status: "ok"` or `insufficient_history` per analytics contract with an empty `candidates` list and a `limitations` reason, not a false ranking

### Requirement: get_file_history tool contract

The system SHALL expose a `get_file_history` MCP tool that accepts validated `repository_id`, `file_path`, and optional bounded `limit`, and returns the file's commit history ordered by date with per-commit evidence and pagination metadata.

#### Scenario: File history returns bounded commit list

- **WHEN** `get_file_history` is called with a valid repository, path, and `limit: 10`
- **THEN** the response has `status: "ok"`, `repository_id`, `target`, a `commits` array of at most 10 entries each with `hash`, `date`, `message`, `author`, `is_fix`, `fix_reason`, `additions`, `deletions`, `change_type`, and pagination fields (`limit_applied`, `total_count` or `has_more`)

#### Scenario: Commit evidence is traceable

- **WHEN** the returned `commits` are inspected
- **THEN** each commit's `hash` is the full commit hash, `date` is the original commit date, and `is_fix`/`fix_reason` reflect the heuristic fix label stored by ingestion, preserving the evidence chain

#### Scenario: File history respects validation and not_found

- **WHEN** `get_file_history` is called with an invalid `limit` or a traversal `file_path` or an unknown repository/file
- **THEN** validation error or `not_found` is returned with `isError: true` before any unbounded graph query

### Requirement: Structured output envelope with evidence and limitations

The system SHALL return every successful MCP tool result in the common analytics envelope containing `status`, `repository_id`, `target` (normalized file path or query scope), result values, `evidence`, and `limitations`/`data_coverage`, keeping numeric results separate from evidence-sufficiency and never presenting missing evidence as zero risk.

#### Scenario: Envelope shape on success

- **WHEN** any of the five tools succeeds
- **THEN** the JSON response contains `status`, `repository_id`, `target`, the tool-specific `result` fields, an `evidence` object or array with commit hashes, counts, and relationship evidence, and a `limitations` object or string describing data coverage and caveats

#### Scenario: Insufficient history is explicit in envelope

- **WHEN** a tool has insufficient history for a file (e.g., shallow graph, filtered paths, parser failures affecting the target)
- **THEN** the envelope has `status: "insufficient_history"` or `"not_found"` with a `limitations` reason that discloses the insufficiency; no numeric score is implied to mean safety

### Requirement: Structured error mapping and failure isolation

The system SHALL map known domain failures to structured MCP tool errors with `isError: true` and an `error.code`/`message` that is actionable and does not leak secrets, driver internals, or raw Cypher, and SHALL ensure a single tool's failure does not crash the server or affect other concurrent calls.

#### Scenario: Graph unavailable returns structured error

- **WHEN** a tool is invoked while Neo4j is unreachable or the configured database is unavailable
- **THEN** the tool returns `status: "graph_unavailable"` or `"internal_error"` with code `GRAPH_UNAVAILABLE` and a retry hint, `isError: true`, and the server process remains running for subsequent calls

#### Scenario: Failure isolation across tools

- **WHEN** one tool call fails validation or encounters a graph error
- **THEN** a concurrent call to a different tool on the same server instance succeeds independently if its inputs and dependencies are valid

#### Scenario: No secrets in errors or logs

- **WHEN** any tool validation or graph error is returned or logged
- **THEN** the message does not contain `NEO4J_USERNAME`, `NEO4J_PASSWORD`, API keys, raw connection strings, or interpolated Cypher with secrets

### Requirement: Health and readiness behaviour

The system SHALL provide a health/readiness check that distinguishes process liveness from dependency readiness by verifying Neo4j connectivity via the graph driver, and tool calls when not ready SHALL return a structured not-ready error rather than crashing or falsely reporting success.

#### Scenario: Health when Neo4j reachable

- **WHEN** the health/readiness check is queried while Neo4j is reachable and the configured database is accessible
- **THEN** it reports `status: "ok"` with `neo4j: "ok"` and the MCP server is considered ready for tool calls

#### Scenario: Not ready when Neo4j unreachable

- **WHEN** the health/readiness check is queried while Neo4j is unreachable
- **THEN** it reports `status: "degraded"` or not-ready with `neo4j: "unavailable"` and tool invocations return `graph_unavailable` instead of marking the server as ready

### Requirement: Concurrency and deterministic results

The system SHALL handle concurrent tool calls safely without sharing mutable request state or leaking sessions, and SHALL return deterministic results for the same graph snapshot and configuration.

#### Scenario: Concurrent calls succeed

- **WHEN** multiple MCP clients invoke different tools concurrently against the same server
- **THEN** all calls complete successfully with correct per-call results and no session leaks or cross-request contamination

#### Scenario: Deterministic output for fixed graph

- **WHEN** the same tool with the same arguments is called twice against an unchanged graph and configuration
- **THEN** the responses are byte-identical (same scores, rankings, evidence ordering)

### Requirement: Security constraints — parameterized Cypher and no arbitrary query execution

The system SHALL use only parameterized Cypher via the graph layer's fixed query templates for all reads and SHALL NOT expose arbitrary Cypher execution nor accept a model-generated query string as a database query; MCP tools SHALL call named analytics/graph operations only.

#### Scenario: Special characters in file_path are parameterized

- **WHEN** `file_path` contains characters that would alter Cypher semantics if interpolated (e.g., `'`, `;--`, `MATCH`, `DETACH DELETE`)
- **THEN** the value is passed as a Cypher parameter and queried literally without injection or query failure

#### Scenario: No arbitrary Cypher endpoint

- **WHEN** a caller attempts to supply a raw Cypher string or query field to any tool
- **THEN** the tool has no such parameter, unknown fields are rejected as validation errors, and no Cypher is executed

#### Scenario: No model-generated query string

- **WHEN** the MCP server receives tool arguments from the agent
- **THEN** it validates and forwards them to named operations only; the server never executes a `query` argument as Cypher
