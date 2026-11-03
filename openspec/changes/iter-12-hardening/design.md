# Design: iter-12-hardening — Testing, Backtesting, Reliability, and Release Readiness

## Context

See `proposal.md` — Why for motivation. Iterations 0–11 are assumed complete: config (Pydantic v2, `chronotrace.yml`, env secrets), graph (Neo4j driver, schema migrations, parameterised Cypher), ingestion (PyDriller, path filtering, fix labelling), static analysis (AST import resolution), analytics (decay, scoring, evidence contracts), MCP (five tools, Streamable HTTP), agent (Meta Model API, tool loop, iteration bound), API (FastAPI lifespan, health/readiness, chat/report/ingest), Docker Compose (three services, named volumes), CLI (eight commands, `api_client`/`docker` helpers), and Web UI (chat/report/graph views). Current constraints shaping this iteration: `pyproject.toml` declares `pytest`, `pytest-cov`, `ruff`, `mypy` under `[project.optional-dependencies].dev` and `testpaths = ["tests"]`; `chronotrace.yml` uses `decay_half_life_days: 365`, file-scoped analysis, and glob exclusions; `docker-compose.yml` exposes Neo4j on `7687`/`7474` with `neo4j_data` volume; stability risk is silent regressions and unmeasured ranking utility rather than missing features.

## Goals / Non-Goals

**Goals:**

- Verify every iteration's contracts through isolated unit tests, integrated service tests, and mocked-agent tests so regressions surface before the demo.
- Establish a reproducible, honestly reported backtest that measures whether pre-T rankings correlate with post-T fix activity and compares fairly to a trivial baseline.
- Harden security boundaries (secrets, code execution, query parameterization, input validation) and reliability boundaries (timeouts, bounds, deterministic fallbacks) without changing public API/CLI/MCP signatures.
- Document a clean-environment demo path and known limitations so reviewers can reproduce the result and interpret evidence correctly.

**Non-Goals:**

- New product capabilities, new endpoints, new containers, or new user-facing commands — all product surfaces are exercised, not extended.
- Rebuilding Neo4j schema, analytics formulae, or agent prompts — only their test coverage and hardening, not their functional redesign (changes limited to minimal safety fixes).
- Broad performance optimization, load testing beyond the backtest dataset, or multi-language/embedding/vector features listed as MVP exclusions in PLAN §7.
- Automated enforcement of coverage gates in CI beyond a documented `make test` / `pytest --cov` command and a release checklist.

## Decisions

### Decision: Layered test organization with fixture-driven determinism

Keep `tests/unit/` for pure-Python deterministic modules, `tests/integration/` for Neo4j/MCP/API/Compose touch-points, `tests/e2e/` for the full bring-up path, and optionally `tests/agent/` for the model-loop if it does not fit cleanly under `integration`. Store reusable graph snapshots and small fixture repos under `tests/fixtures/`. Unit tests use no network/DB; integration tests use a disposable Neo4j (Compose or test container) and `httpx`/`TestClient` mocks where the real service is not under test. Shared helpers (graph seeding, decay helpers) live in `tests/conftest.py` or `tests/fixtures/helpers.py`, not in `src/`.

- Alternatives considered: single `tests/` flat layout — rejected because it hides which suite needs Docker/Neo4j and forces slow runs for fast feedback loops. Duplicating analytics logic in test-side reimplementations — rejected; tests call the real `src/chronotrace/analytics` against fixture graphs instead.

### Decision: Backtest as a thin reproducible script over existing graph/analytics

Implement the backtest as a Python script (e.g., `scripts/backtest.py` or `src/chronotrace/backtest/runner.py` used only as a tool) that, for each evaluation point T: (1) checks out or filters the graph to pre-T history, (2) calls the existing analytics ranking unchanged, (3) records the post-T fix window ground truth via PyDriller fix labels, (4) computes the target metric and the same-window baseline ranking, and (5) writes `docs/backtest.md` plus a machine-readable `reports/backtest.json`. The script reuses `core/ingestion`, `core/static_analysis`, `core/graph`, and `core/analytics` directly — it does not fork rankings or re-derive scores.

- Alternatives considered: standalone backtest framework with its own storage — rejected as duplication; using the live API for rankings — rejected for speed and determinism, direct analytics calls are more reproducible. Picking a different target (e.g., bug-inducing commits via SZZ) — rejected per PLAN §7 exclusion; fix-commit co-occurrence is the MVP-feasible proxy.

### Decision: Baseline is raw churn or bug-fix count from the same pre-T slice

The required baseline comparison is intentionally trivial: rank files by pre-T raw change count or by pre-T heuristic fix-count, then score the same post-T target. It uses the identical evaluation points, graph slice, and window as the ChronoTrace ranking. No learning, no weighting search, no hyperparameter tuning is introduced in this iteration.

- Alternatives considered: random baseline or popularity baseline — less informative than churn/fix-count, which is the natural "would a simple count do better?" question reviewers will ask.

### Decision: Minimal hardening fixes, maximal documentation

Security/reliability fixes in this iteration are intentionally narrow: redact secrets from logs/errors, enforce list-form `subprocess` for Git/Docker, ensure every Cypher call is parameterised, add path/URL/limit validation at API and MCP boundaries, enforce agent iteration and payload bounds, and confirm deterministic report availability on model failure. Each fix is a small patch to the existing module — no new auth system, no new middleware layer. Behaviour that is already safe is left as-is and covered by tests rather than rewritten. Data-residency and limitations go into `README.md`/`docs/limitations.md`/`docs/backtest.md`, not into new runtime components.

- Alternatives considered: introducing a secrets vault or policy engine — deferred as out-of-scope for an MVP hardening pass.

### Decision: Demo reproducibility is a checklist, not a framework

The clean install-to-report path is validated by a written `docs/demo.md` (or `README` section) plus a single end-to-end smoke test that exercises `init` → `up` (or Compose directly in CI) → `add` with a small fixture → `report` → `chat` → `down`. The smoke test is not a full user acceptance harness; it proves the stack wires together from a clean state. Reviewers follow the same steps manually for the live demo.

## Risks / Trade-offs

- **Flaky integration tests on CI without Docker/Neo4j** → Mitigation: unit suite runs everywhere with no external deps; integration/e2e suites are marked `pytest.mark.integration` and skip gracefully when Docker/Neo4j is unavailable, with a CI job that does provide them. No suite pretends to pass when the dependency is missing.
- **Backtest small-sample overinterpretation** → Mitigation: report always includes sample size, window, and a limitations paragraph that labels small/biased runs as exploratory; headline metric is qualified with the dataset name and window and never claimed as general prediction.
- **Time-sliced graph filtering is expensive** → Mitigation: backtest operates on commit-date-filtered queries or temporary filtered reads rather than destroying the main graph; evaluation points are sampled (not exhaustive over all commits) to keep runtime bounded, and this sampling is disclosed.
- **Fixture repo choice biases results** → Mitigation: choose a moderately active Python repo with tests and multi-month history, document why it was chosen, and note that results are repo-specific; do not generalize in the report or the demo narrative.
- **Hardening patches regress existing flows** → Mitigation: every hardening patch ships with its own unit/integration test (e.g., path traversal, secret redaction, parameterised Cypher) so the fix is proven not to break the happy path.
- **Secret scan false positives / committed secret history** → Mitigation: scan looks at both tracked files and `git log`; if an old secret is found, rotate it and document the rotation rather than rewriting history, and add a `.gitleaks` or `secrets` ignore for known non-secrets.

## Migration Plan

No schema or data migration is required. Deployment steps:

1. Merge the test/backtest/docs additions; no Compose or image change is required beyond a rebuild if hardening patches touched the backend image.
2. Rebuild the backend image only if `src/` hardening patches are present: `docker compose build`.
3. Run the verification sequence: `pytest tests/unit` → `pytest -m integration` (with Neo4j/Compose) → `python scripts/backtest.py` (or `make backtest`) → manual `init` → `up` → `add` → `report` → `chat` → `down` smoke.
4. Publish the backtest report under `docs/backtest.md` and reference it from `README.md` and `docs/`.

Rollback: revert the added tests/scripts/docs and any minimal hardening patches; no persistent data format changes to undo. Neo4j volumes are untouched.

## Open Questions

- Which specific demo Python repository to use for the backtest (candidates: a mid-size open-source Python project with 500+ commits and real tests, e.g., `requests`, `flask`, or a curated fixture). Resolved at implementation time by picking one and documenting the choice; does not change specs or task breakdown.
- Exact backtest window (N days) and K for precision@K — documented as configurable defaults in the script header and report, not baked into analytics code.
