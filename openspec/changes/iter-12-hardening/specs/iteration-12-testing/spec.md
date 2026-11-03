# Spec Delta

## Purpose

Establishes the testing contracts that prove ChronoTrace's deterministic core, service integrations, and agent loop remain correct, isolated, and reproducible before any release.

## ADDED Requirements

### Requirement: Unit test coverage for deterministic core

The system SHALL provide unit tests that cover configuration parsing and validation, path normalization and filtering, bug-fix regex matching and revert detection, rename identity decisions, AST parsing and import resolution, time-decay calculations, analytics scoring and ranking, result contracts with insufficient-history behaviour, API request/response validation, and CLI argument handling, and SHALL run them without requiring Neo4j, Docker, or network access.

#### Scenario: Unit suite passes in isolation

- **WHEN** `pytest tests/unit` (or `make test-unit`) is run on a machine with no Neo4j or Docker
- **THEN** all unit tests pass and failures name the specific module and assertion that failed

#### Scenario: Configuration validation edge cases are tested

- **WHEN** unit tests exercise missing file, malformed YAML, unsupported version, unknown keys, invalid regex, invalid numeric values, tilde-path resolution, and non-writable directories
- **THEN** each case asserts the expected actionable error type and message without leaking secret values

#### Scenario: Path filtering and normalization are tested

- **WHEN** unit tests exercise POSIX and Windows-style paths against glob exclusion patterns, including edge cases such as `**/.git/**`, nested directories, and mixed separators
- **THEN** filtering decisions match the documented canonical-path rules and tests assert inclusion/exclusion explicitly

#### Scenario: Analytics scoring remains deterministic

- **WHEN** unit tests invoke analytics operations (change criticality, regression risk, historical impact, test recommendation, time decay, normalization, ranking) against fixed graph fixtures
- **THEN** outputs are deterministic, scores include component breakdowns and evidence, insufficient-history cases return the explicit status rather than zero-risk, and duplicate evidence is not double-counted

### Requirement: Integration tests for graph and ingestion

The system SHALL provide integration tests that verify Neo4j schema initialization, idempotent graph writes, repository ingestion into Neo4j, import-graph persistence, all five MCP tools, FastAPI↔MCP connectivity, and Docker Compose startup and shutdown, running against a disposable Neo4j instance or Compose stack where required.

#### Scenario: Schema initialization is idempotent

- **WHEN** integration tests run schema initialization twice against the same Neo4j instance
- **THEN** constraints and indexes remain present and no duplicate-constraint errors occur

#### Scenario: Ingestion and import graph are verified end-to-end

- **WHEN** integration tests ingest a small fixture repository and rebuild the static import graph
- **THEN** commit, author, file, and modification nodes are repository-scoped, excluded paths are absent, rename evidence is preserved per the identity policy, and import edges match the expected fixture

#### Scenario: All five MCP tools are exercised

- **WHEN** integration tests call `get_change_criticality`, `get_regression_risk`, `get_historical_impact`, `recommend_tests`, and `get_file_history` against the demo graph through the MCP transport
- **THEN** each tool validates inputs, bounds result limits, prevents path traversal, returns structured evidence with limitations, and reports unknown repository/file as a structured error rather than a crash

#### Scenario: Docker Compose lifecycle is tested

- **WHEN** integration tests (or the e2e Compose smoke test) bring the stack up and down
- **THEN** `neo4j` becomes reachable via the Compose hostname, API readiness reflects dependency health, and `down` without `--volumes` preserves named volumes

### Requirement: Agent behavior tests with mocked model

The system SHALL provide agent tests that use mocked model responses to verify tool-call routing, rejection of malformed tool arguments, correct handoff of tool results back to the model, iteration-limit enforcement, no evidence invention when tool results are empty or incomplete, and graceful handling of provider failures.

#### Scenario: Tool-call routing is verified

- **WHEN** a mocked model response contains a valid tool call for one of the five MCP tools
- **THEN** the agent validates the tool name and arguments, invokes the correct MCP tool, and returns the tool output in the provider-required format

#### Scenario: Malformed arguments are rejected

- **WHEN** a mocked model response contains a tool call with missing, mistyped, or out-of-bounds arguments
- **THEN** the agent rejects the call with an actionable error, does not invoke the tool, and surfaces the rejection without fabricating evidence

#### Scenario: Iteration limit prevents loops

- **WHEN** mocked model responses repeatedly request tool calls up to and beyond the configured maximum iteration count
- **THEN** the agent stops at the limit, returns a deterministic error or partial result indicating the limit was reached, and does not enter an unbounded loop

#### Scenario: Agent does not invent evidence

- **WHEN** tool results are empty, partial, or report `insufficient_history`
- **THEN** the agent states that evidence is insufficient, does not cite file paths, commit hashes, dates, counts, or scores absent from the tool output, and distinguishes observed association from causation where required

#### Scenario: Provider failures are handled deterministically

- **WHEN** the model provider returns a timeout, rate-limit, invalid-credential, or malformed-response error
- **THEN** the agent surfaces a deterministic, actionable error without fabricating an answer, and deterministic report paths remain available when applicable

### Requirement: Test organization and reproducibility

The system SHALL organize tests under `tests/unit/`, `tests/integration/`, and `tests/e2e/` (or `tests/agent/` where cohesive), keep fixtures under `tests/fixtures/`, support `pytest` and `make test` invocation, report coverage via `pytest-cov` where configured, and document how to run each suite and interpret results.

#### Scenario: Suites can be run independently

- **WHEN** a developer runs `pytest tests/unit`, `pytest tests/integration`, or `pytest tests/e2e` separately
- **THEN** each suite runs only its intended scope and reports pass/fail per test with no hidden cross-suite ordering dependency

#### Scenario: Coverage and fixtures are available

- **WHEN** tests run with coverage enabled
- **THEN** the report includes deterministic modules (config, ingestion helpers, static analysis, analytics, API schemas, CLI) and fixtures include stable graph snapshots for ranking comparisons
