# Spec Delta

## Purpose

Hardens ChronoTrace for release by enforcing security boundaries, reliability behaviour under failure, and demonstrable end-to-end readiness from a clean environment.

## ADDED Requirements

### Requirement: Secrets are never exposed

The system SHALL keep API keys and database passwords out of source control, logs, exception messages, image layers, and committed configuration files, and SHALL require them only via environment variables or an untracked env file.

#### Scenario: Secrets do not appear in repository

- **WHEN** a secret scan (e.g., `git log --all -p`, `grep` for key patterns, or a configured scanner) is run against the repository
- **THEN** it finds no `MODEL_API_KEY`, `NEO4J_PASSWORD`, or other credential values committed to history or present in `chronotrace.yml` examples

#### Scenario: Errors do not leak secrets

- **WHEN** configuration or startup validation fails due to a missing or invalid secret
- **THEN** the error names the missing variable (e.g., `MODEL_API_KEY`) without printing its value

#### Scenario: Image layers do not contain secrets

- **WHEN** the backend Docker image is inspected (e.g., `docker history`, layer diff, or build log review)
- **THEN** no layer bakes in an API key or database password; secrets are supplied only at runtime via env or env file

### Requirement: No repository code execution and safe process invocation

The system SHALL NOT execute repository code during Git ingestion or static AST parsing, and SHALL invoke Git and Docker operations via safe subprocess APIs that do not interpolate user-supplied URLs or paths into shell command strings.

#### Scenario: Ingestion does not execute repository code

- **WHEN** a repository containing executable hooks, `setup.py`, or malicious Python is ingested
- **THEN** no file under the cloned repository is executed; only Git history and AST parsing occur

#### Scenario: Git URL is not shell-interpolated

- **WHEN** a user supplies a crafted repository URL containing shell metacharacters
- **THEN** the system treats it as a literal string argument to the Git/HTTP operation, does not pass it through a shell, and rejects it via URL validation if unsupported

### Requirement: Parameterised queries and no arbitrary Cypher exposure

The system SHALL use parameterised Cypher for all graph reads and writes and SHALL NOT expose arbitrary Cypher execution through MCP tools or the HTTP API.

#### Scenario: All Cypher is parameterised

- **WHEN** graph-layer code is inspected
- **THEN** every query passes values as parameters (e.g., `$repoId`, `$filePath`) rather than interpolating strings into Cypher

#### Scenario: No arbitrary query tool exists

- **WHEN** the MCP server's advertised tool list and the API's route table are inspected
- **THEN** no tool or endpoint accepts a raw Cypher string from the caller; only named analytics operations with validated arguments are available

### Requirement: Input validation and path traversal prevention

The system SHALL validate repository URLs against supported transport formats, validate and bound all file-path and limit inputs, normalize paths with the canonical rules, and prevent path traversal in repository-relative paths.

#### Scenario: Invalid repository URL is rejected

- **WHEN** an API or CLI caller submits an unsupported URL or an empty string as a repository URL
- **THEN** the request is rejected with a 400-class structured error naming the invalid value and supported formats, without performing a fetch

#### Scenario: Path traversal is blocked

- **WHEN** a caller supplies a file path containing `..`, absolute segments that escape the repository root, or null bytes
- **THEN** the request is rejected with an actionable error and no filesystem access outside the repository root occurs

#### Scenario: Result limits are bounded

- **WHEN** a caller requests `get_file_history`, `get_regression_risk`, or any bounded result with an excessive or missing limit
- **THEN** the system applies a documented maximum (or default) and truncates or rejects the request rather than returning unbounded data

### Requirement: Bounded agent iterations and graceful model-failure handling

The system SHALL bound agent tool-call iterations, bound tool result sizes, handle model rate limits, timeouts, and outages with deterministic errors, and keep deterministic reports available when the model is unavailable.

#### Scenario: Agent iteration limit is enforced under failure

- **WHEN** the model repeatedly issues tool calls or the provider times out during a chat request
- **THEN** the agent stops at the configured iteration/timeout bound, returns a deterministic error indicating the cause, and does not fabricate evidence

#### Scenario: Deterministic fallback remains available

- **WHEN** the model is unavailable but the graph and analytics layers are healthy
- **THEN** `report` and direct analytics endpoints continue to serve deterministic results without requiring the model

### Requirement: Data-residency and content disclosure

The system SHALL document what repository metadata is stored locally (graph, cloned repos, logs) and what, if anything, is sent to the hosted model provider, and SHALL avoid sending repository file content to the model unless required by the explicit user query and disclosed.

#### Scenario: Documentation discloses data flows

- **WHEN** a user reads `README.md` or `docs/` for privacy/data handling
- **THEN** the docs state which fields are persisted locally, which fields are sent to the model provider during chat, and that ingestion does not transmit repository content by default

### Requirement: Clean install-to-report demo reproducibility

The system SHALL support a documented clean-environment demo workflow (`init` → `up` → `add <repo>` → `report`/`chat` → `status`/`down`) that succeeds from a fresh checkout with only Docker/Compose prerequisites, and SHALL surface evidence and limitations visibly in agent and UI responses.

#### Scenario: Clean demo succeeds

- **WHEN** a reviewer follows the documented demo steps from a clean checkout after `docker compose down -v` (or equivalent reset)
- **THEN** initialization creates config and directories, services become ready, repository ingestion completes, a deterministic report is retrievable, and chat returns an evidence-grounded answer or an explicit insufficient-history notice

#### Scenario: Evidence and limitations remain visible

- **WHEN** the API, agent, or UI renders results in the demo (criticality scores, regression evidence, impact, test recommendations)
- **THEN** each result includes evidence citations (paths, commit hashes, dates, counts) and, where coverage is thin, a visible limitation/insufficient-history notice rather than silent zeros

### Requirement: Known limitations are documented

The system SHALL document known limitations and unsupported cases (non-Python, dynamic imports, namespace packages, shallow history, excluded paths, tiny backtest samples) so users do not misinterpret missing evidence as zero risk.

#### Scenario: Limitations are discoverable

- **WHEN** a user reads the project's `README.md` or `docs/` limitations section
- **THEN** the listed limitations match the constraints stated in PLAN.md sections 4–5 and iteration 12, and no documented limitation contradicts observed system behaviour
