# Tasks

## 1. Configuration models and exceptions

- [x] 1.1 Define `src/chronotrace/config/exceptions.py` hierarchy (`ChronoTraceConfigError`, `ConfigValidationError`, `MissingSecretError`, `FilesystemError`, `ConnectivityError`) with redacted `__str__` and verify `pytest tests/unit/test_config_exceptions.py -k redaction` passes
- [x] 1.2 Implement `src/chronotrace/config/validators.py` helpers (positive int/float, null-or-positive, regex `re.compile` probe, enum literal checks) and verify unit tests `tests/unit/test_config_validators.py` cover valid/invalid cases
- [x] 1.3 Implement `src/chronotrace/config/models.py` Pydantic v2 models (`ChronoTraceConfig`, `ProjectConfig`, `HistoryConfig`, `FilteringConfig`, `BugFixLabelsConfig`, `GraphConfig`, `AgentConfig`) with `version: Literal[1]`, `extra="forbid"`, `frozen=True`, field defaults from `chronotrace.yml`, and verify `tests/unit/test_config_models.py` loads the example YAML and rejects unsupported version/unknown keys/invalid numerics/enums/regex
- [x] 1.4 Export public symbols from `src/chronotrace/config/__init__.py` (`ChronoTraceConfig`, `load_config`) and verify `python -c "from chronotrace.config import load_config, ChronoTraceConfig; print(load_config)"` imports without FastAPI/CLI

## 2. Loader, precedence, and path handling

- [x] 2.1 Implement `find_config_file` and YAML parsing in `src/chronotrace/config/loader.py` (search `./chronotrace.yml` â†’ `~/.chronotrace/chronotrace.yml`, distinct errors for missing file vs malformed YAML with path/line) and verify `tests/unit/test_config_loader.py::test_missing_file` and `test_malformed_yaml` pass with actionable messages
- [x] 2.2 Implement tilde expansion, `Path.resolve()`, directory creation (`mkdir(parents=True, exist_ok=True)`), and writability probe (trial temp file) for `project.data_dir`/`repositories_dir` and verify `tests/unit/test_config_paths.py` covers Windows/POSIX tilde, creation, and non-writable failure via mocked probe
- [x] 2.3 Implement secret overlay from `env`/`python-dotenv` (never YAML) for `MODEL_API_KEY`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` plus optional `MODEL_BASE_URL`/`MODEL_NAME`, reject YAML secrets as unknown keys, and verify `tests/unit/test_config_secrets.py` passes for present/missing/leaked-value cases (no secret in error output)
- [x] 2.4 Implement deterministic precedence `cli_overrides > env > file > defaults` in `load_config(config_path?, env?, cli_overrides?)` with documented env key map (e.g., `CHRONOTRACE_DATA_DIR`, `CHRONOTRACE_BATCH_SIZE`) and verify `tests/unit/test_config_precedence.py` asserts each layer wins over the next

## 3. Startup validation and connectivity

- [x] 3.1 Implement `validate_startup(config, env, check_neo4j: bool)` that separates parsing from connectivity â€” checks required secrets before agent/graph init, checks filesystem before ingestion, and verify `tests/unit/test_config_startup.py` passes for missing `MODEL_API_KEY` / `NEO4J_*` / non-writable dir without opening a DB connection (mocked driver)
- [x] 3.2 Implement Neo4j connectivity check (driver `verify_connectivity()` + `RETURN 1` in configured `database`, proper `driver.close()`, map to `ConnectivityError` vs `ConfigValidationError` for bad URI/auth/database vs timeout/refused) and verify `tests/unit/test_config_neo4j.py` with mocked `neo4j` driver distinguishes config error from unavailable DB
- [x] 3.3 Ensure API/agent readiness gating (startup report `ok: bool` blocks serving `ready` when secrets/filesystem/Neo4j fail, no secret leakage) and verify `tests/unit/test_config_readiness.py` asserts not-ready status and redacted logs
- [x] 3.4 Enforce single public loader contract: no module outside `chronotrace.config` reads YAML/env directly; add import-lint check or grep test and verify `tests/unit/test_config_single_loader.py` (or `rg -n "yaml\.safe_load|os\.environ" src/chronotrace --glob '!src/chronotrace/config/**'`) finds zero violations

## 4. CLI/API wiring and documentation

- [x] 4.1 Wire `src/chronotrace/cli/main.py` `--config PATH` option through `load_config` and surface actionable errors via `Rich` without leaking secrets, and verify `python -m chronotrace --help` and `python -m chronotrace --config /tmp/nonexistent.yml version` produce expected error via subprocess test
- [x] 4.2 Add/update `.env.example` and ensure `chronotrace.yml` example validates against schema v1, and verify `python -c "from chronotrace.config import load_config; load_config('chronotrace.yml')"` succeeds on the committed example
- [x] 4.3 Add API startup dependency that calls `validate_startup` before graph/agent initialization (no agent request without validated `MODEL_API_KEY`) and verify `tests/unit/test_config_api_guard.py` blocks unvalidated agent calls before network use

## 5. Integration and quality gates

- [x] 5.1 Add/expand unit tests to cover all spec scenarios (missing file, malformed YAML, unsupported version, unknown key, invalid regex/numerics/enums, tilde creation, non-writable, missing secrets, precedence, readiness, single-loader) achieving full iteration-1 coverage and verify `pytest tests/unit -k config --cov=chronotrace.config` passes
- [x] 5.2 Run project linters/formatters and type checks (`ruff check .`, `ruff format --check .`, `mypy src/chronotrace/config`) and verify zero errors in changed files
- [x] 5.3 Run `openspec validate --change iter-1-config --strict` and fix any warnings (purpose length, requirement description, scenario WHEN/THEN, missing deltas) and verify it exits 0
