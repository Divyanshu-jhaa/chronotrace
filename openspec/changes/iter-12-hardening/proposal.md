# Proposal: iter-12-hardening — Testing, Backtesting, Reliability, and Release Readiness

## Why

Iterations 0–11 deliver the full ChronoTrace backend and user interfaces (CLI, API, MCP, agent, Docker, Web UI) but leave test coverage uneven, no reproducible evidence that rankings are useful, and no systematic security or reliability sweep before public demonstration. Iteration 12 validates the system as a whole — comprehensive tests, a documented backtest with baseline comparison, and a hardened release — so the hackathon demo can be reproduced from a clean environment without hidden failures or leaked secrets.

## What Changes

- Expand unit test coverage to all deterministic modules: configuration parsing/validation, path normalization and filtering, bug-fix regex and revert detection, rename identity decisions, AST import resolution, time-decay, analytics scoring/ranking, result contracts and insufficient-history behaviour, API request/response validation, and CLI argument handling.
- Expand integration tests to: Neo4j schema initialization, idempotent graph writes, repository ingestion into Neo4j, import-graph persistence, all five MCP tools, FastAPI↔MCP connectivity, and Docker Compose startup/shutdown lifecycle.
- Add agent tests with mocked model responses: tool-call routing, malformed argument rejection, correct handoff of tool results, iteration-limit enforcement, no-evidence-invention when tool results are empty, and provider-failure handling.
- Establish a reproducible backtest methodology on a selected demo Python repository: time-sliced evaluation points, rankings computed only from history before each point, measurable target (e.g., whether highly-ranked files appear in subsequent fix commits within a stated window), comparison against a simple baseline (raw churn or bug-fix count), and a documented report of dataset, time window, sample size, metric, limitations, and one honest headline metric.
- Execute a security and reliability sweep: no secrets in source control/logs/image layers, no repository code execution during ingestion/static parsing, safe subprocess invocation, parameterised Cypher only, no arbitrary Cypher exposure, URL/path validation, path-traversal prevention, bounded tool result sizes and agent iterations, graceful handling of model rate limits/timeouts/outages, deterministic fallback when the model is unavailable, data-residency disclosure, clean install-to-report demo reproducibility, and documentation of known limitations.
- Make the release demo-ready: end-to-end suites pass, backtest is reproducible, a clean `init` → `up` → `add` → `report`/`chat` flow succeeds, evidence and limitations remain visible in agent/UI responses, and security checks report no findings.

## Capabilities

### New Capabilities

- `iteration-12-testing`: Comprehensive test strategy and coverage contracts for unit, integration, and agent layers, including required test subjects, isolation/mocking expectations, and pass criteria for the hardened system.
- `iteration-12-backtest`: Reproducible backtesting methodology that evaluates ChronoTrace rankings against future fix evidence, compares to a simple baseline, and reports dataset, window, sample size, metric, limitations, and a single honest headline metric.
- `iteration-12-hardening`: Security, reliability, and release-readiness contracts — secret handling, safe execution boundaries, input validation, resource bounds, failure-mode handling, deterministic fallbacks, data-residency disclosure, and documented demo readiness.

### Modified Capabilities

- None — no existing `openspec/specs` capabilities are modified by this iteration; earlier capabilities (config, graph, ingestion, static-analysis, analytics, MCP, agent, API, Docker, CLI, UI) remain behaviorally unchanged and are exercised by the new tests without altering their spec requirements.

## Impact

- **Code:** No new product features; test additions under `tests/unit/`, `tests/integration/`, `tests/e2e/` and fixture helpers under `tests/fixtures/`; a backtest script or module (e.g., `scripts/backtest.py` or `src/chronotrace/backtest/`) plus a documented report under `docs/backtest.md`; optional hardening fixes confined to existing modules (e.g., secret redaction, input validation, bounds) without introducing new APIs or containers.
- **APIs/Services:** No new HTTP endpoints; existing health/readiness/chat/report/ingest contracts are validated more thoroughly and their failure modes hardened.
- **Dependencies:** `pytest`, `pytest-cov`, `ruff`, `mypy` already declared; optional dev helpers for coverage thresholds or backtest notebooks; no new runtime dependencies required — current stack (`fastapi`, `neo4j`, `pydriller`, `mcp`, `httpx`, `typer`, `rich`) unchanged.
- **Systems:** Neo4j (schema idempotency, write isolation), Docker Compose lifecycle, model provider API (rate limits/timeouts), and the filesystem (path traversal, config secrets); browser UI verification remains through the existing API, not direct DB access.
- **Tests/Tooling:** Unit/integration/agent suites, fixtures for deterministic graph snapshots, CI-friendly `make test` / `pytest` invocation with coverage reporting, a `make backtest` or equivalent reproducible command, and manual security-check scripts (e.g., secret scan, dependency audit) integrated into the release checklist.
- **Breaking changes:** None — purely additive verification and hardening; no change to public CLI, API, or MCP tool signatures.
- **Security:** Hardens existing secret, query, and execution boundaries; no new secret stores or privilege escalation; disclosures added to `README.md`/`docs/` rather than code-level secret handling changes.
