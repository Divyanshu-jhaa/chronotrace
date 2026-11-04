# api Specification

## Purpose

Provides the stable FastAPI HTTP interface for CLI and UI clients — application lifespan with startup validation, health vs readiness, repository ingestion with observable job tracking, chat backed by the agent, and deterministic reports backed by analytics — with thin routes, typed schemas, and clean shutdown.

## Requirements

### Requirement: Application lifespan and startup validation

The system SHALL provide a FastAPI application with an async lifespan that on startup loads and validates `chronotrace.yml` via the single Iteration 1 loader, validates directories, verifies Neo4j connectivity and model config before marking services ready, initializes shared driver/analytics/MCP client/agent, and on shutdown closes drivers and clients. Startup SHALL fail when required config or a hard dependency is unavailable.

#### Scenario: Successful startup when all dependencies are available

- **WHEN** the app starts with a valid `chronotrace.yml`, writable `data_dir`/`repositories_dir`, reachable Neo4j at `graph.uri`/`database`, and present `MODEL_API_KEY`/`NEO4J_*` secrets
- **THEN** lifespan startup completes, the app accepts requests, and `GET /ready` returns `status: ready`

#### Scenario: Startup fails on invalid configuration

- **WHEN** `chronotrace.yml` is missing, malformed, has an unsupported version, unknown key, invalid regex, or invalid numeric value
- **THEN** startup raises a validation error with an actionable message, the process exits or lifespan fails, and no endpoint reports ready

#### Scenario: Startup fails or stays not-ready when Neo4j is unreachable

- **WHEN** Neo4j at the configured `uri`/`database` is unreachable or authentication fails during startup verification
- **THEN** the app does not report `ready`; startup either fails fast or `GET /ready` returns `status: not_ready` with `neo4j: unavailable` and `503`

#### Scenario: Shutdown closes shared resources

- **WHEN** the app receives SIGTERM or lifespan shutdown is triggered with in-flight requests
- **THEN** the system closes the Neo4j driver, MCP client, and model/http clients, drains in-flight work with a bounded timeout, and does not leak sessions

### Requirement: Health endpoint — process liveness only

The system SHALL expose `GET /health` that reports process health with no dependency checks. It SHALL return `200` with `{status: ok, version, uptime}` when the process is running, regardless of Neo4j or model availability.

#### Scenario: Health returns ok while dependencies are down

- **WHEN** `GET /health` is called while Neo4j is unreachable but the FastAPI process is running
- **THEN** it returns `200` with `status: ok` and does not report dependency status

#### Scenario: Health payload shape

- **WHEN** `GET /health` succeeds
- **THEN** the response contains `status`, `version` (from package metadata), and `uptime` or `started_at`, and no secrets

### Requirement: Readiness endpoint — dependency health

The system SHALL expose `GET /ready` (or `/readiness`) that reports dependency health including `neo4j`, `config`, and `agent/model` reachability. It SHALL return `200` with `status: ready` only when all hard dependencies are verified; otherwise it SHALL return `503` with `status: not_ready` and per-dependency details.

#### Scenario: Ready is ready when dependencies pass

- **WHEN** config is valid, Neo4j `verify_connectivity` succeeds, and required model config is present
- **THEN** `GET /ready` returns `200` with `status: ready` and `checks: {neo4j: ok, config: ok, agent: ok}`

#### Scenario: Ready is not_ready when Neo4j is down

- **WHEN** `GET /ready` is called while Neo4j is unreachable
- **THEN** it returns `503` with `status: not_ready` and `checks.neo4j: unavailable`

#### Scenario: Health and readiness are distinct

- **WHEN** a caller probes `GET /health` and `GET /ready` during a Neo4j outage
- **THEN** health is `200 ok` while readiness is `503 not_ready`

### Requirement: Typed request and response schemas

The system SHALL define Pydantic v2 request and response schemas for every endpoint in `src/chronotrace/api/schemas/`. Schemas SHALL enforce required fields, types, and constraints, reject malformed JSON and, where strict, unknown fields, and SHALL never include secrets or raw Cypher in responses.

#### Scenario: Missing required field is rejected

- **WHEN** `POST /chat` is sent without `repository_id`
- **THEN** the system returns `422` with the shared error envelope and an actionable message naming the missing field

#### Scenario: Unknown field handling

- **WHEN** a request body contains an unknown field and the schema is strict
- **THEN** validation fails with `422` and the error message lists the offending key

#### Scenario: Secret not exposed in any response

- **WHEN** any endpoint returns a response or error
- **THEN** the body does not contain `MODEL_API_KEY`, `NEO4J_PASSWORD`, connection strings, or driver internals

### Requirement: Structured error envelope

The system SHALL return all `4xx`/`5xx` errors in a shared envelope `{error: {code, message, details?}, request_id?}` with consistent HTTP codes: `422` for validation, `404` for not_found, `503` for not_ready/graph_unavailable/model_unavailable, `500` for internal. Messages SHALL be actionable and SHALL not leak secrets or driver internals. FastAPI `RequestValidationError` and `HTTPException` SHALL be mapped to this envelope.

#### Scenario: Validation error envelope

- **WHEN** a client sends an invalid Git URL to `POST /repositories`
- **THEN** the system returns `422` with `error.code: validation_error` and a message describing the URL requirement

#### Scenario: Not-found envelope

- **WHEN** `GET /repositories/unknown-id` or `POST /chat` with unknown `repository_id` is called
- **THEN** the system returns `404` with `error.code: not_found` and preserves `repository_id` in the message without leaking internal state

#### Scenario: Graph unavailable maps to 503

- **WHEN** an endpoint needs Neo4j but the driver raises a connection error
- **THEN** the system returns `503` with `error.code: graph_unavailable` and a retry hint

### Requirement: Repository registration and ingestion — validation and non-blocking trigger

The system SHALL expose `POST /repositories` that validates the Git URL as a non-empty HTTP/HTTPS or SSH URL with no shell interpolation, creates a `repository_id` and ingestion job, triggers ingestion without blocking the event loop, and returns `201` with `{repository_id, job_id, status: queued|running}`.

#### Scenario: Valid URL creates job and returns immediately

- **WHEN** `POST /repositories` is called with `url: https://github.com/org/repo.git`
- **THEN** it returns `201` within 500 ms (in test with mocked ingestion) with `repository_id`, `job_id`, and `status: queued` or `running`

#### Scenario: Invalid Git URL is rejected

- **WHEN** `POST /repositories` is called with an empty string, a non-URL, or a string containing shell metacharacters intended for interpolation
- **THEN** it returns `422` with `validation_error` and does not create a job

#### Scenario: Ingestion does not block event loop

- **WHEN** ingestion of a large repository is triggered
- **THEN** the HTTP handler returns immediately and the heavy work runs in a background task or `asyncio.to_thread`, observable via the job endpoint

### Requirement: Repository listing and status

The system SHALL expose `GET /repositories` for listing known repositories and `GET /repositories/{repository_id}` (plus `GET /jobs/{job_id}` if supported) for inspecting a single repository and its ingestion job. List responses SHALL include `{repository_id, url, ingestion_status, last_ingested_at, head}`; detail responses SHALL include job `status`, `stage`, `progress`, timestamps, and `error` when failed.

#### Scenario: List repositories

- **WHEN** `GET /repositories` is called after registering two repositories
- **THEN** it returns `200` with an array containing both `repository_id`/`url` entries and their ingestion statuses

#### Scenario: Status shows running then completed

- **WHEN** a repository's ingestion job is polled via `GET /repositories/{id}` or `GET /jobs/{job_id}`
- **THEN** successive polls show `status: queued|running` with `stage` and then `status: completed` with `finished_at` and persisted `head` or `last_ingested_at`, or `failed` with `error`

#### Scenario: Unknown repository returns 404

- **WHEN** `GET /repositories/does-not-exist` is called
- **THEN** the system returns `404` with `error.code: not_found`

### Requirement: Job tracking

The system SHALL track ingestion jobs in a `JobStore` with fields `job_id` (UUID), `repository_id`, `status` (`queued|running|completed|failed`), `stage` (e.g., `CLONING|EXTRACTING|FILTERING|WRITING_GRAPH|COMPLETED|FAILED`), `progress`, `error`, `created_at`/`updated_at`, and SHALL make the store async-safe and bounded. Jobs SHALL transition monotonically through the status machine and SHALL be queryable by both repository and job identifiers.

#### Scenario: Job transitions are observable

- **WHEN** an ingestion job is created and progresses
- **THEN** its stored `status` moves `queued → running → completed` (or `failed`) and `updated_at` advances on each transition

#### Scenario: Failed job does not mark repository updated

- **WHEN** an ingestion job fails (clone error, extraction error, write-graph error)
- **THEN** the job status is `failed` with `error` and the repository's `last_successful_ingestion` timestamp is not advanced

#### Scenario: Concurrent job access is safe

- **WHEN** two callers create or poll jobs concurrently
- **THEN** the `JobStore` handles concurrent `create`/`update`/`get` without corruption or lost updates

### Requirement: Chat endpoint — repository-scoped agent delegation

The system SHALL expose `POST /chat` for repository-scoped agent queries, requiring an explicit valid `repository_id`, delegating to the Iteration 7 Agent via MCP with iteration and time limits, and returning `{answer, citations, evidence, limitations, conversation_id}`. Model or MCP failures SHALL map to `503`/`504`, not fabricated answers.

#### Scenario: Successful chat returns evidence-grounded answer

- **WHEN** `POST /chat` is called with a known `repository_id` and a valid `question`
- **THEN** the system calls the agent, which calls MCP tools, and returns `200` with `answer` containing citations to `repository_id` evidence and `evidence`/`limitations` fields

#### Scenario: Missing repository_id is rejected

- **WHEN** `POST /chat` is called without `repository_id` or with an empty string
- **THEN** the system returns `422` with `validation_error`

#### Scenario: Unknown repository returns 404

- **WHEN** `POST /chat` is called with `repository_id: unknown`
- **THEN** the system returns `404` with `not_found`

#### Scenario: Model unavailable returns 503 not fabricated answer

- **WHEN** the model provider is unavailable (invalid credentials, timeout, rate limit) during chat
- **THEN** the system returns `503` with `error.code: model_unavailable` and does not synthesize repository facts

### Requirement: Report endpoint — deterministic analytics without LLM

The system SHALL expose `POST /reports` (or `GET /reports` with query params) that accepts `{repository_id, file_path}` where `file_path` is a repository-relative POSIX path validated against traversal and absolute-path rules, and SHALL return the Iteration 5 common contract `{status: ok|insufficient_history|not_found, repository_id, target, result, evidence, limitations}` by calling analytics/report services directly. It SHALL work when the model is unavailable and SHALL NOT invoke the LLM.

#### Scenario: Report returns analytics result when file exists

- **WHEN** `POST /reports` is called with a known `repository_id` and `file_path: src/foo.py` that exists with sufficient history
- **THEN** it returns `200` with `status: ok`, `result` containing score/breakdown/rankings, `evidence`, and `limitations`

#### Scenario: Traversal path is rejected

- **WHEN** `POST /reports` is called with `file_path: ../secret.py` or `/etc/passwd` or `a\b\c.py`
- **THEN** it returns `422` with `validation_error` and does not query the graph

#### Scenario: Report works without model

- **WHEN** `MODEL_API_KEY` is unset or the model client would fail
- **THEN** `POST /reports` still succeeds for a valid `repository_id`/`file_path` because it bypasses the agent/model

#### Scenario: Insufficient history is explicit

- **WHEN** a report is requested for a file with insufficient history (e.g., one commit, no co-change)
- **THEN** the result has `status: insufficient_history` or `status: ok` with a limitation stating insufficiency, not a zero-risk claim

### Requirement: Thin routes — no business logic in handlers

The system SHALL keep route handlers in `src/chronotrace/api/routes/` thin: they SHALL contain only HTTP validation, dependency wiring, service delegation, and response shaping. They SHALL NOT contain Cypher queries, scoring formulae, Git subprocess calls, or model prompt construction; that logic SHALL reside in `core/graph`, `core/analytics`, `core/ingestion`, `agent/`, or `mcp` services.

#### Scenario: Route delegates to service

- **WHEN** `POST /reports` handler is reviewed and tested with a mocked analytics service
- **THEN** it validates inputs and delegates to `analytics_service.get_change_criticality` (or report facade) and maps the return to the HTTP response without computing scores

#### Scenario: No Cypher in routes

- **WHEN** the `routes/` directory is scanned
- **THEN** it contains no Cypher strings, `session.run`, or `MATCH` queries; all graph access goes through the graph layer

### Requirement: Non-blocking event loop

The system SHALL ensure API request handlers do not block the asyncio event loop with synchronous long-running work. Ingestion `CLONING|EXTRACTING|FILTERING|WRITING_GRAPH` work SHALL run in a background task or via `asyncio.to_thread`, and a request that triggers ingestion SHALL return within the documented p50 bound while work continues.

#### Scenario: Handler returns while ingestion runs

- **WHEN** `POST /repositories` triggers ingestion mocked to take 5 seconds
- **THEN** the HTTP response arrives within 500 ms and the job is in `running` state

#### Scenario: Concurrent requests remain responsive

- **WHEN** one ingestion job is running and a second client calls `GET /health` or `POST /reports`
- **THEN** the second call returns without waiting for ingestion to finish

### Requirement: Clean shutdown

The system SHALL implement a lifespan shutdown that stops accepting new requests, drains in-flight requests with a bounded timeout, closes the Neo4j driver (`driver.close()`), closes the MCP and model `httpx` clients (`aclose`), and cancels or awaits background jobs. Resources SHALL not leak after shutdown.

#### Scenario: Driver and clients are closed on shutdown

- **WHEN** lifespan shutdown is triggered after successful startup
- **THEN** the test spy asserts `neo4j_driver.close()` and `mcp_client.aclose()`/`model_client.aclose()` were called exactly once and sessions are not reusable afterward

#### Scenario: Shutdown timeout bounded

- **WHEN** shutdown is triggered with a long-running ingestion job still `running`
- **THEN** the system waits at most the configured shutdown timeout (e.g., 5–10 s) before cancelling the job and marking it `failed` with a shutdown reason

### Requirement: Security — no secret leakage and bounded inputs

The system SHALL never expose database credentials, `MODEL_API_KEY`, connection strings, or driver internals in API responses or error messages, SHALL validate and bound all inputs (URL length, question length, `file_path` length, JSON body size), and SHALL enforce path-traversal prevention via canonical POSIX checks.

#### Scenario: Error message redacts secrets

- **WHEN** Neo4j authentication fails or a downstream error includes credentials
- **THEN** the returned `error.message` is generic ("authentication failed", "graph unavailable") and contains no password, key, or URI credentials

#### Scenario: Oversized payload is rejected

- **WHEN** a client sends a `question` exceeding the documented limit (e.g., 10k chars) or a JSON body exceeding the size limit
- **THEN** the system returns `413` or `422` with `validation_error` and does not process the payload

#### Scenario: Path traversal via encoded separators is rejected

- **WHEN** `file_path` contains `..`, `%2e%2e%2f`-decoded `..`, backslash, or null byte
- **THEN** validation rejects it with `422` before any graph query
