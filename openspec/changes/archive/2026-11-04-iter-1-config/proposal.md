# Proposal: iter-1-config — Configuration Loading, Schema, and Startup Validation

## Why

ChronoTrace has no validated configuration interface; every backend component needs a single source of truth for filesystem paths, analysis parameters, graph connectivity, and agent secrets. Without Pydantic-validated `chronotrace.yml` (schema v1), env-secret handling, path expansion, and startup checks, later iterations (ingestion, graph, analytics, MCP, agent) would independently re-read YAML/env and fail inconsistently. Iteration 1 establishes the only supported way to load, validate, and distribute configuration.

## What Changes

- Define `chronotrace.yml` schema v1 with Pydantic v2 models for `version`, `project`, `analysis` (including `history`, `filtering`, `bug_fix_labels`), `graph`, and `agent` sections. Reject unsupported `version` values and unknown keys with actionable errors.
- Validate field-level constraints at load time: positive `batch_size` and `decay_half_life_days`; `max_commits` as `null | positive int`; `exclude` and `bug_fix_labels.patterns` as compilable regexes; enumerations for `language` (`python`), `granularity` (`file`), `graph.provider` (`neo4j`), `agent.provider` (`meta`), and `reasoning_effort` (`low`/`medium`/`high`).
- Load runtime secrets exclusively from environment variables (`MODEL_API_KEY`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`); never read secrets from YAML. Support configurable `MODEL_BASE_URL` / `MODEL_NAME` via env or validated config. Require model API key before agent initialization and Neo4j credentials before graph services.
- Expand `~` in `project.data_dir` / `project.repositories_dir`, resolve to absolute paths, create directories if missing, and verify writability. Preserve POSIX/Windows path normalization for `exclude` matching.
- Establish precedence: explicit CLI option > environment variable > user config file (`chronotrace.yml`) > built-in defaults, and expose a single public loader `load_config(...)` that returns a typed, immutable config object.
- Implement startup validation that runs before dependent services: YAML structure/values, required secrets, filesystem paths, and Neo4j connectivity/database access (distinguishing config failure from unavailable DB where practical). Do not mark API/agent as ready if dependencies unavailable; never leak secret values in logs or exceptions.
- Keep config parsing separate from connectivity checks (parsing must succeed without a live Neo4j).
- **BREAKING**: Unknown top-level or nested keys are rejected (strict schema); callers relying on permissive YAML must update.

## Capabilities

### New Capabilities

- `config`: Validated configuration loading and startup checks — Pydantic v2 schema v1 for `chronotrace.yml`, env-secret handling, path expansion/creation, value validation, precedence, filesystem and Neo4j startup validation, single public loader, and safe error reporting.

### Modified Capabilities

- None — no prior `openspec/specs` capabilities exist (flat spec store is empty).

## Impact

- **Code**: `src/chronotrace/config/` (`models.py`, `loader.py`, `validators.py`, `exceptions.py`, `__init__.py`) — new/rewritten; `src/chronotrace/__init__.py` version source; `chronotrace.yml` and `.env.example` as reference fixtures.
- **APIs/Contracts**: New `load_config(config_path?, env_overrides?) -> ChronoTraceConfig` public interface consumed by CLI, API, ingestion, graph, and agent. No HTTP API changes in this iteration.
- **Dependencies**: `pydantic>=2`, `pyyaml`, `python-dotenv`, `typer`, `rich` (already in `pyproject.toml`). No new infra services.
- **Systems**: Filesystem (data/repos dirs), Neo4j driver (connectivity check only, no schema writes), process env. Docker/Compose unchanged except env wiring already present.
- **Tests/Tooling**: Unit tests for success/failure cases (missing file, malformed YAML, unsupported version, unknown key, invalid regex/numerics, tilde expansion, writable check, missing secrets, precedence). No provider network calls in unit tests.
