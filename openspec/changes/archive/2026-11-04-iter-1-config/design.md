# Design: iter-1-config — Configuration Loading, Schema, and Startup Validation

## Context

Iteration 0 delivered a minimal `src`-layout package (`chronotrace==0.1.0`), Typer CLI with `version`, and empty `src/chronotrace/config/` stubs (`models.py`, `loader.py`, `validators.py`, `exceptions.py`). There is no validated config interface; `chronotrace.yml` in the repo root is illustrative but not enforced. Later iterations (ingestion → graph → analytics → MCP → agent → API) all need project paths, history/filter settings, graph connection, and agent/model secrets. This design establishes the only supported way to load, validate, and distribute configuration before any of that work starts.

Constraints: Pydantic v2, `pyyaml`, `python-dotenv` already in `pyproject.toml`; no new infra; must work on Windows/macOS/Linux; secrets never in YAML or logs; config parsing must not require a live Neo4j; see `proposal.md` — Why for motivation.

## Goals / Non-Goals

**Goals:**

- Provide one typed, immutable `ChronoTraceConfig` consumed by CLI, API, ingestion, graph, and agent.
- Enforce strict `chronotrace.yml` schema v1 with actionable, non-secret-leaking errors for every invalid input.
- Implement deterministic precedence (CLI > env > file > defaults) and a single public loader.
- Expand/resolve/create/verify `data_dir` and `repositories_dir` on all three OSes.
- Gate service readiness on startup checks (YAML → secrets → filesystem → Neo4j connectivity) with distinguishable failure modes.

**Non-Goals:**

- No HTTP endpoints, Neo4j schema/migrations, ingestion, static analysis, analytics, MCP, agent, or UI work (later iterations).
- No encrypted secret storage, rotation, or vault integration — env-only for MVP.
- No hot-reload / file-watching of `chronotrace.yml` — restart to pick up changes.
- No new Docker images or Compose `mcp-server`/`agent-api` services beyond the existing `neo4j` service in `docker-compose.yml`.

## Decisions

### Decision 1: Pydantic v2 models as single source of truth

Pydantic `BaseModel` with `ConfigDict(extra="forbid", frozen=True, validate_assignment=True)` defines `ChronoTraceConfig`, `ProjectConfig`, `HistoryConfig`, `FilteringConfig`, `BugFixLabelsConfig`, `GraphConfig`, `AgentConfig`, and a top-level `version: Literal[1]`. `field_validator` / `Annotated` with `AfterValidator` handles positivity, regex compilation, and enum checks; `extra="forbid"` surfaces unknown keys as `ValidationError`.

- Alternatives considered: `dataclasses` + manual validation; `pydantic-settings` for everything. Rejected: first duplicates validation Pydantic already does; second mixes env precedence opaquely — explicit precedence is clearer for CLI overrides and testing.
- Risks mitigated: strictness prevents silent drift; frozen model prevents post-load mutation bugs.

### Decision 2: Two-phase loader — parse then startup-check

`loader.py` exposes `load_config(config_path?: Path, env: Mapping = os.environ, cli_overrides?: dict) -> ChronoTraceConfig` and `validate_startup(config) -> StartupReport`.

- Phase 1 (parse): YAML load → Pydantic validation → tilde expansion (`Path.expanduser()`) → resolve absolute (`Path.resolve()`) → mkdir parents → writability probe (`os.access` + trial temp file creation) → env-secret overlay (see below). No network I/O.
- Phase 2 (startup): verify required secrets present; verify Neo4j connectivity via `neo4j.GraphDatabase.driver(uri, auth=(user, password)).verify_connectivity()` and session `RETURN 1` against `database` name. Parsing success does not imply readiness.
- Alternative: single pass that does everything. Rejected: unit tests need to exercise parsing without a live DB.

### Decision 3: Secret handling — env-only, dotenv for local dev

Secrets `MODEL_API_KEY`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` are read from `env` (injected by Docker/CI or by `load_dotenv()` in `loader` when `env` is real process env). Optional runtime overrides `MODEL_BASE_URL`, `MODEL_NAME` similarly via env with defaults (`https://api.meta.ai/v1`, `muse-spark-1.3`) validated as URL/model string. No `secrets:` section in YAML; any such key hits `extra="forbid"`. Exception types `MissingSecretError` / `ConfigValidationError` never embed values; `repr`/`str` are redacted.

- Alternative: YAML secrets with file permissions. Rejected: violates PLAN principle "Secrets must not be committed or stored in YAML".
- Library: `python-dotenv` only called when `env` is `os.environ` and `.env` exists; tests inject a dict so no filesystem side effect.

### Decision 4: Path handling is cross-platform by construction

Use `pathlib.Path.expanduser` + `resolve` + `mkdir(parents=True, exist_ok=True)` and normalize `filtering.exclude` patterns via `PurePosixPath` matching (ingestion in iter-3 will reuse the same normalization). Writability check: `tempfile.TemporaryFile` probe inside target dir (covers Windows ACL vs `os.access` quirks). Regex patterns are stored as strings but validated via `re.compile` in validators.

- Alternative: `os.path.expanduser` only. Rejected: `Path` gives stricter typing and better Windows handling.

### Decision 5: Deterministic precedence implemented explicitly

Order applied at field resolution: built-in defaults (Pydantic `Field(default=...)`) → user file values → env overrides → `cli_overrides` dict. Env mapping uses a canonical key map (e.g., `CHRONOTRACE_DATA_DIR`, `CHRONOTRACE_BATCH_SIZE`, `MODEL_API_KEY`, `NEO4J_URI`) documented in `loader.py` docstring and `.env.example`. This makes precedence testable by injecting dicts; no hidden `pydantic-settings` magic.

- Alternative: Rely on pydantic-settings `env_prefix`. Rejected: obscures CLI override layer and makes unit tests require env monkeypatching.

### Decision 6: Content-addressed location and public surface

```
src/chronotrace/config/
  __init__.py        # re-exports load_config, ChronoTraceConfig, exceptions
  models.py          # Pydantic models
  validators.py      # reusable validators (positive_int, regex, enums)
  loader.py          # load_config, validate_startup, find_config_file
  exceptions.py      # ConfigError, ValidationError, MissingSecretError, FilesystemError, ConnectivityError
```

CLI (`src/chronotrace/cli/main.py`) accepts `--config PATH` and forwards to `load_config`; API dependencies inject the singleton config. No module beyond `config` reads YAML/env directly (enforced by review + import lint).

## Data Model (Pydantic)

```python
ChronoTraceConfig(version: Literal[1], project, analysis, graph, agent)
ProjectConfig(name: str, data_dir: Path, repositories_dir: Path)
AnalysisConfig(language: Literal["python"], granularity: Literal["file"],
               include_tests: bool, history: HistoryConfig,
               filtering: FilteringConfig, bug_fix_labels: BugFixLabelsConfig)
HistoryConfig(max_commits: int | None, decay_half_life_days: float)  # >0
FilteringConfig(exclude: list[str])  # each compilable
BugFixLabelsConfig(case_sensitive: bool, patterns: list[str])  # each compilable
GraphConfig(provider: Literal["neo4j"], uri: str, database: str, batch_size: int)  # >0
AgentConfig(provider: Literal["meta"], model: str, reasoning_effort: Literal["low","medium","high"])
# Secrets are NOT in ChronoTraceConfig fields; they live in ResolvedSecrets
# returned alongside or fetched via get_secrets(config, env)
```

Defaults: `language=python`, `granularity=file`, `include_tests=true`, `max_commits=null`, `decay_half_life_days=365`, `provider=neo4j`, `database=neo4j`, `batch_size=500`, `reasoning_effort=medium`; `data_dir`/`repositories_dir` default to `~/.chronotrace/...` (expanded). YAML missing optional sections fills defaults; presence of extra keys raises.

## Interfaces

- `load_config(config_path: Path | None, env: Mapping[str,str]=os.environ, cli_overrides: dict | None) -> ChronoTraceConfig` — locates file (`./chronotrace.yml` → `~/.chronotrace/chronotrace.yml` if not explicit), handles missing-file/malformed errors, applies precedence, performs path expansion/creation.
- `validate_startup(config: ChronoTraceConfig, env: Mapping) -> StartupReport` / `raise StartupError` — checks secrets, filesystem, and if requested `check_neo4j=True`, connectivity. Returns structured report with `ok: bool, errors: list[FieldError], warnings: list`.
- `find_config_file(explicit: Path | None) -> Path` — resolution helper.
- Exceptions: all subclass `ChronoTraceConfigError`; messages include `field_path` and `hint` but never secret values.

## Security

- No secret ever written to disk, YAML, or logs. Error formatter redacts values whose field name contains `api_key`, `password`, `secret`, `token`.
- YAML unknown-key rejection prevents typos that could be mistaken for secret keys.
- Neo4j driver closed via context manager; exceptions mapped to `ConnectivityError` vs `ConfigValidationError` (bad URI/auth/database name vs timeout/refused).

## Risks / Trade-offs

- [Strict schema rejects old permissive files] → Mitigation: actionable error lists allowed keys; versioned schema (`version: 1`) allows future evolution with migration note.
- [Windows `~` and writability semantics differ] → Mitigation: `expanduser` + trial-file probe tested on all three OSes; unit tests mock writability and integration test checks real temp dir.
- [Neo4j not running during unit tests] → Mitigation: Phase 1 tests never connect; Phase 2 tests mock driver or gate behind `pytest.mark.integration`.
- [Regex validation at load vs. use] → Mitigation: fail fast at load; store original strings so diagnostics can report exact pattern.
- [Precedence confusion for users] → Mitigation: documented order plus `chronotrace config show --effective` (deferred to CLI iteration; for now logged at debug level without secrets).
- [Frozen Pydantic model prevents post-load path fixup] → Mitigation: expansion/creation happens before model construction or via `mode="before"` validators; resolved Paths are final.

## Migration Plan

- No data migration. New install: `pip install -e .` → copy `chronotrace.yml` (example) → set env in `.env` from `.env.example` → run `python -m chronotrace --help` → loader self-test.
- Existing installs: no prior validated config to migrate; any hand-written YAML that relied on permissive parsing will now error — fix by removing unknown keys or pinning `version: 1`.
- Rollback: revert package; `chronotrace.yml` is backward-compatible for strict v1 readers.

## Open Questions

- Exact env var names for non-secret overrides (`CHRONOTRACE_DATA_DIR` vs `CHRONOTRACE_PROJECT_DATA_DIR`) — finalize during implementation; spec already covers precedence behavior regardless of prefix.
- Whether `agent.model` should be further constrained to a known allow-list vs free string — defer to model integration iteration; for now validate non-empty string and let runtime fail on unknown model.

