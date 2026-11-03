# Tasks

## 1. Test inventory and harness setup

- [ ] 1.1 Audit existing tests and fixtures under `tests/` and list covered vs missing subjects from the iteration-12-testing spec, and verify the inventory is written to a checklist or coverage plan referenced in the PR
- [ ] 1.2 Set up `tests/conftest.py` and `tests/fixtures/` helpers for deterministic graph seeding, fixture repo checkout, and decay/ranking helpers so unit tests run without Neo4j/Docker and integration tests can seed a disposable graph, and verify `pytest tests/unit --collect-only` lists the new fixtures
- [ ] 1.3 Configure `pytest` markers (`unit`, `integration`, `e2e`) and a `make test` / `Makefile` target that runs `pytest tests/unit` by default and `pytest -m integration` when requested, and verify `pytest -m unit` and `pytest -m integration --collect-only` both succeed
- [ ] 1.4 Document how to run each suite (`tests/unit`, `tests/integration`, `tests/e2e`) and interpret coverage output in `README.md` or `docs/testing.md`, and verify the documented command runs as written on a fresh checkout

## 2. Unit tests for deterministic core

- [ ] 2.1 Add unit tests for configuration parsing and validation (missing file, malformed YAML, unsupported version, unknown keys, invalid regex/numerics, tilde paths, writability, secret-missing errors without value leakage) and verify `pytest tests/unit/test_config* -v` passes with no DB or network
- [ ] 2.2 Add unit tests for path normalization, glob filtering, and co-change/bug-fix label helpers (POSIX/Windows paths, `**/.git/**` edge cases, case-insensitive fix regex, revert detection, rename identity rules) and verify `pytest tests/unit/test_path* tests/unit/test_labels* -v` passes
- [ ] 2.3 Add unit tests for AST import resolution and static-analysis diagnostics (absolute/relative imports, `__init__.py`, stdlib/third-party exclusion, parse failures, unresolved import reporting, idempotent edge writes) and verify `pytest tests/unit/test_static* -v` passes
- [ ] 2.4 Add unit tests for time decay, analytics scoring/ranking, and result contracts (half-life decay, normalization, weighted scoring, duplicate-evidence handling, deterministic fixtures, `insufficient_history` / `not_found` statuses) and verify `pytest tests/unit/test_analytics* tests/unit/test_decay* -v` passes and fixtures produce stable expected scores
- [ ] 2.5 Add unit tests for API schema validation and CLI argument handling (request/response schemas, invalid payloads, `chronotrace add/chat/report` argument errors, `--config`/`--api-url` overrides, path-traversal rejection) and verify `pytest tests/unit/test_api* tests/unit/test_cli* -v` passes

## 3. Integration tests for graph, ingestion, MCP, and Compose

- [ ] 3.1 Add integration tests for Neo4j schema initialization and idempotent writes (constraint/index presence, repeated `MERGE` without duplicates, repository-scoped identifiers, error mapping for DB failures) and verify `pytest -m integration tests/integration/test_graph* -v` passes against a disposable Neo4j
- [ ] 3.2 Add integration tests for repository ingestion and import-graph persistence (clone/fetch, filtering, rename handling, fix-label reason, revert linkage, batch writes, retry/idempotency, re-checkout snapshot import rebuild) and verify `pytest -m integration tests/integration/test_ingest* tests/integration/test_import* -v` passes
- [ ] 3.3 Add integration tests for all five MCP tools and FastAPI↔MCP connectivity (tool validation, limit bounds, path traversal, structured evidence and error shapes, concurrent calls, DB-failure mapping, transport reachability) and verify `pytest -m integration tests/integration/test_mcp* tests/integration/test_api* -v` passes with the MCP server reachable
- [ ] 3.4 Add a Compose lifecycle smoke test (`up`/`status`/`down` preserves volumes, readiness reflects dependency health) exercised via `tests/e2e/test_compose.py` or `make smoke`, and verify the smoke test passes when Docker is available and is skipped gracefully otherwise

## 4. Agent behavior tests

- [ ] 4.1 Add mocked-model agent tests for tool-call routing and argument validation (valid dispatch to each of the five tools, rejection of missing/mistyped/out-of-bounds args without invoking the tool) and verify `pytest tests/unit/test_agent* -k routing -v` passes with no network
- [ ] 4.2 Add mocked-model tests for result handoff, iteration-limit enforcement, and no-evidence-invention (tool results returned in provider format, max iterations stops with deterministic error, empty/insufficient-history yields explicit limitation without invented paths/hashes) and verify `pytest tests/unit/test_agent* -k "iteration or evidence or handoff" -v` passes
- [ ] 4.3 Add mocked-model tests for provider failures (timeout, rate limit, invalid credentials, malformed response) and deterministic fallback path for reports, and verify `pytest tests/unit/test_agent* -k failure -v` passes and the report path serves without the model

## 5. Backtest methodology and report

- [ ] 5.1 Select and document the demo Python repository, time window, and evaluation-point sampling strategy in a design note or `docs/backtest.md` stub, and verify the chosen repo URL, commit range rationale, and sampling rule are stated explicitly
- [ ] 5.2 Implement the reproducible backtest runner (e.g., `scripts/backtest.py` or `src/chronotrace/backtest/runner.py`) that for each evaluation point T uses only pre-T history to compute ChronoTrace rankings and the same-slice baseline (raw churn or fix-count), and verify a dry run on a tiny fixture produces side-by-side results with no post-T leakage
- [ ] 5.3 Define and implement the measurable target and metric (e.g., precision@K or hit rate for top-K files appearing in fix commits within N days) with sample-size handling and window configuration, and verify the metric computes correctly on a hand-checked fixture
- [ ] 5.4 Generate the full backtest report at `docs/backtest.md` (and optionally `reports/backtest.json`) containing dataset, evaluation-point count, window, metric definition, ChronoTrace vs baseline side-by-side table, one honest headline metric qualified with dataset/window, and a limitations paragraph, and verify re-running the documented command reproduces the headline metric within tolerance

## 6. Security and reliability sweep

- [ ] 6.1 Sweep secrets out of source, logs, and image layers (scan committed files and `git log`, remove any credential values, confirm `.env.example`/`chronotrace.yml.example` contain placeholders only, redact secret values from errors) and verify a secret scan finds no `MODEL_API_KEY` or `NEO4J_PASSWORD` values and Docker history shows no baked secrets
- [ ] 6.2 Harden execution and query boundaries (no repo code execution, list-form subprocess for Git/Docker, 100% parameterised Cypher, no raw-Cypher tool/endpoint) and verify by code review plus a grep check that no string-interpolated Cypher remains and no shell-interpolated URL remains
- [ ] 6.3 Harden input validation and resource bounds (URL transport validation, path traversal prevention, limit defaults/maximums, agent iteration and payload size bounds, timeout and rate-limit handling, deterministic report availability on model outage) and verify with targeted tests that traversal, excessive limits, and model outages produce the specified structured errors without crashes
- [ ] 6.4 Document data-residency and content disclosure (what is stored locally vs sent to the model provider, when repo content leaves the machine) in `README.md` or `docs/privacy.md`, and verify the disclosure matches the actual `agent/model_client.py` and `mcp` data flows

## 7. Release readiness and final validation

- [ ] 7.1 Document known limitations and unsupported cases (non-Python, dynamic/namespace imports, shallow history, excluded paths, tiny backtest caveat) in `README.md` or `docs/limitations.md`, and verify the list matches PLAN §7 and the backtest report's limitations section without contradiction
- [ ] 7.2 Document the clean install-to-report demo path in `README.md` or `docs/demo.md` (`init` → `up` → `add <repo>` → `report`/`chat` → `status`/`down` with prerequisites and expected evidence/limitation visibility), and verify following the steps from a clean checkout after `docker compose down -v` succeeds and renders evidence citations
- [ ] 7.3 Run the full release validation sequence — `pytest tests/unit`, `pytest -m integration` (with Neo4j/Compose), `python scripts/backtest.py` (or `make backtest`), and the clean `init`→`report` smoke — and verify all suites pass, the backtest report is present and honestly qualified, evidence/limitations are visible in agent/UI responses, and no security check reports findings; fix any failures before closing the iteration
