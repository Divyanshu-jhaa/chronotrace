# config Specification

## Purpose

Provides the validated, precedence-aware configuration interface for ChronoTrace — Pydantic v2 `chronotrace.yml` schema v1, environment-secret handling, path expansion, and startup validation used by every backend component.

## Requirements

### Requirement: Pydantic v2 schema v1 models validated configuration

The system SHALL define the supported `chronotrace.yml` schema as Pydantic v2 models covering `version`, `project` (`name`, `data_dir`, `repositories_dir`), `analysis` (`language`, `granularity`, `include_tests`, `history`, `filtering`, `bug_fix_labels`), `graph` (`provider`, `uri`, `database`, `batch_size`), and `agent` (`provider`, `model`, `reasoning_effort`), and SHALL reject unsupported schema versions with an actionable error.

#### Scenario: Valid v1 config loads

- **WHEN** a well-formed `chronotrace.yml` with `version: 1` and valid sections is loaded via the public loader
- **THEN** the system returns a typed, immutable `ChronoTraceConfig` instance with all fields populated

#### Scenario: Unsupported version rejected

- **WHEN** a file declares `version: 2` (or any value other than `1`)
- **THEN** loading fails with an actionable error naming the unsupported version and the supported value

#### Scenario: Complete example from repo validates

- **WHEN** the committed `chronotrace.yml` example (project/history/filtering/bug_fix_labels/graph/agent) is loaded
- **THEN** validation succeeds and typed fields match expected defaults (e.g., language python, granularity file)

### Requirement: Unknown keys are rejected with actionable errors

The system SHALL reject any unknown top-level or nested key in `chronotrace.yml` (strict/forbid-extra) and SHALL produce an actionable error identifying the offending key path.

#### Scenario: Unknown top-level key rejected

- **WHEN** YAML contains an unknown key such as `unknown_field: true` at the top level
- **THEN** validation fails with an error that names the unknown key and indicates strict schema rejection

#### Scenario: Unknown nested key rejected

- **WHEN** YAML contains an unknown nested key such as `analysis.unknown_nested: 1`
- **THEN** validation fails with an error identifying the nested key path

### Requirement: File existence and YAML syntax are validated

The system SHALL report a missing configuration file and malformed YAML as distinct actionable errors including the filesystem path and parse location.

#### Scenario: Missing file reported

- **WHEN** the configured `chronotrace.yml` path does not exist and no inline/default is available
- **THEN** loading fails with an error stating the file is missing and showing the resolved absolute path

#### Scenario: Malformed YAML reported

- **WHEN** the file contains invalid YAML (e.g., unclosed bracket or bad indentation)
- **THEN** loading fails with a YAML parse error including line/column context

### Requirement: Numeric constraints are validated

The system SHALL validate that `graph.batch_size` and `analysis.history.decay_half_life_days` are positive numbers (> 0) and that `analysis.history.max_commits` is either `null` or a positive integer, rejecting invalid values with actionable errors.

#### Scenario: Non-positive batch_size rejected

- **WHEN** `graph.batch_size` is `0` or negative
- **THEN** validation fails naming `batch_size` and the positive-number constraint

#### Scenario: Non-positive decay_half_life rejected

- **WHEN** `analysis.history.decay_half_life_days` is `0` or negative
- **THEN** validation fails naming `decay_half_life_days` and the constraint

#### Scenario: Invalid max_commits rejected

- **WHEN** `analysis.history.max_commits` is `0`, negative, or a non-integer string
- **THEN** validation fails; `null` and positive integers succeed

### Requirement: Regex patterns are validated at load time

The system SHALL validate that every entry in `analysis.filtering.exclude` and `analysis.bug_fix_labels.patterns` is a compilable regular expression at configuration load time, rejecting invalid patterns with the offending pattern and compile error.

#### Scenario: Invalid exclude pattern rejected

- **WHEN** `analysis.filtering.exclude` contains an invalid regex such as `[unclosed`
- **THEN** loading fails identifying the exact pattern and the regex compile error

#### Scenario: Invalid bug_fix pattern rejected

- **WHEN** `analysis.bug_fix_labels.patterns` contains an invalid regex
- **THEN** loading fails identifying the exact pattern

#### Scenario: Valid patterns compile

- **WHEN** all patterns are valid (e.g., default glob-like excludes and `\bfix\b`)
- **THEN** loading succeeds and patterns are stored as validated strings ready for compilation

### Requirement: Enumeration values are validated

The system SHALL validate enumeration fields against their allowed sets: `analysis.language` is `python`; `analysis.granularity` is `file`; `graph.provider` is `neo4j`; `agent.provider` is `meta`; `agent.reasoning_effort` is one of `low`, `medium`, `high`.

#### Scenario: Unsupported language rejected

- **WHEN** `analysis.language` is `java` or any value other than `python`
- **THEN** validation fails naming the field and the allowed value(s)

#### Scenario: Unsupported reasoning_effort rejected

- **WHEN** `agent.reasoning_effort` is `ultra`
- **THEN** validation fails naming the allowed values `low|medium|high`

### Requirement: Runtime secrets are loaded exclusively from environment variables

The system SHALL load runtime secrets (`MODEL_API_KEY`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`) exclusively from environment variables (and `.env` via `python-dotenv` for local dev), SHALL never read secrets from YAML, and SHALL treat YAML `secrets` keys as unknown-key errors.

#### Scenario: Secrets from env are accepted

- **WHEN** env provides `MODEL_API_KEY`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`
- **THEN** the resolved config holds those values for downstream use

#### Scenario: Secrets in YAML are not supported

- **WHEN** `chronotrace.yml` contains any secret field
- **THEN** validation fails as an unknown key, and file-based secrets are never used

#### Scenario: Configurable model endpoint and identifier via env or validated config

- **WHEN** `MODEL_BASE_URL` / `MODEL_NAME` are set via env or `agent.model` / `agent` endpoint fields in validated config
- **THEN** the resolved config reflects the provided values with URL/format validation

### Requirement: Required secrets are enforced before dependent initialization

The system SHALL require `MODEL_API_KEY` before agent/model initialization and `NEO4J_USERNAME`/`NEO4J_PASSWORD` before any graph-dependent service initialization, failing startup with a named-missing error that does not expose values.

#### Scenario: Missing MODEL_API_KEY blocks agent init

- **WHEN** `MODEL_API_KEY` is absent or empty at agent init
- **THEN** initialization fails with an error naming `MODEL_API_KEY` and stating it is required

#### Scenario: Missing Neo4j credentials block graph services

- **WHEN** `NEO4J_USERNAME` or `NEO4J_PASSWORD` is missing at graph service init
- **THEN** initialization fails naming the missing variable

#### Scenario: No secret value is leaked in errors

- **WHEN** any secret validation fails
- **THEN** no error message, exception, or log contains the secret value

### Requirement: Home-directory path expansion, resolution, creation, and writability

The system SHALL expand leading `~` in `project.data_dir` and `project.repositories_dir` (Windows/macOS/Linux), resolve them to absolute normalized paths, create missing directories, and verify writability, failing validation if a directory is not writable.

#### Scenario: Tilde path expands and resolves

- **WHEN** config sets `data_dir: ~/.chronotrace/data` on any OS
- **THEN** the resolved path is absolute with the home directory expanded and separators normalized

#### Scenario: Missing directories are created

- **WHEN** the resolved `data_dir` or `repositories_dir` does not exist
- **THEN** the system creates the directory tree before marking startup ready

#### Scenario: Non-writable directory fails validation

- **WHEN** the resolved directory exists but is not writable (e.g., read-only parent or permission denied)
- **THEN** validation fails with an actionable error naming the path and the writability check

### Requirement: Clear, non-secret-leaking error messages

The system SHALL emit clear, actionable error messages for every validation failure (file, version, key, regex, numeric, enum, path, secret) that identify the field/path and constraint, and SHALL never include secret values in messages, logs, or stack traces.

#### Scenario: Actionable message on invalid numeric

- **WHEN** `batch_size: -5` triggers validation
- **THEN** the error names `graph.batch_size`, states the constraint, and suggests the fix

#### Scenario: Secret redaction in logs

- **WHEN** any error path would normally echo a secret-bearing field
- **THEN** the output contains the field name but not the value (e.g., `MODEL_API_KEY is required` not its content)

### Requirement: Configuration precedence and single public loader

The system SHALL implement deterministic precedence `explicit CLI option > environment variable > user config file (chronotrace.yml) > built-in default` and SHALL expose a single public loader `load_config(config_path?, env?, cli_overrides?) -> ChronoTraceConfig` that applies this precedence; all other modules SHALL consume the returned object rather than re-reading YAML or env independently.

#### Scenario: Precedence resolves correctly

- **WHEN** a value is set in defaults, user config, env, and CLI overrides simultaneously
- **THEN** the CLI value wins; absent CLI, env wins; absent env, user config wins; otherwise default

#### Scenario: Single loader is the only entry point

- **WHEN** any backend module needs configuration
- **THEN** it imports the validated `ChronoTraceConfig` from the single loader rather than opening YAML or reading env directly (verifiable by import/code review)

### Requirement: Parsing is separated from connectivity checks

The system SHALL keep configuration parsing/validation independent of external connectivity; parsing and filesystem validation SHALL succeed without a live Neo4j or model endpoint, and connectivity checks SHALL run as a distinct startup phase.

#### Scenario: Config parses without live Neo4j

- **WHEN** Neo4j is unreachable but YAML, secrets, and paths are valid
- **THEN** `load_config()` succeeds; only the subsequent connectivity check fails

### Requirement: Startup validation gates service readiness

The system SHALL validate YAML structure/values before starting dependent services, validate secrets before agent/model init, validate filesystem paths before ingestion, and verify Neo4j connectivity/database access before marking graph-dependent services ready; the API/agent SHALL not report ready if required dependencies are unavailable, and where practical connectivity validation SHALL distinguish configuration failure (bad URI/auth/database name) from transient unavailability.

#### Scenario: API not ready when Neo4j unavailable

- **WHEN** startup connectivity check cannot reach Neo4j or the configured database is inaccessible
- **THEN** the service reports not-ready / fails health check and does not serve graph-dependent routes as ready

#### Scenario: Distinguish config error vs unavailable DB

- **WHEN** Neo4j URI is syntactically invalid or auth is wrong versus when Neo4j is simply not running
- **THEN** the error messages differentiate the cases where the driver error permits it (auth/config vs. connection timeout/refused)

#### Scenario: Filesystem validation gates ingestion

- **WHEN** `data_dir` is invalid or non-writable at startup
- **THEN** ingestion does not start and the error names the filesystem pre-condition

### Requirement: Config module is independently usable and authoritative

The configuration module SHALL be importable and usable independently of FastAPI/CLI (no hard import dependency on those frameworks), and other modules SHALL consume the validated configuration object rather than independently reading YAML or environment variables; no agent request SHALL be initiated without a validated `MODEL_API_KEY`.

#### Scenario: Independent import

- **WHEN** `from chronotrace.config import load_config` is used in a plain Python context without FastAPI or CLI installed/running
- **THEN** loading and validation work

#### Scenario: No agent call without validated key

- **WHEN** code attempts an agent/model call without a validated `MODEL_API_KEY` in the resolved config
- **THEN** the call is blocked with a validation error before any network request
