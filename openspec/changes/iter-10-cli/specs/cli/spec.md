# Spec Delta

## Purpose

Provides the primary user-facing terminal interface for ChronoTrace — eight commands, Rich output, and supporting API client and Docker helpers that mediate all service interactions.

## ADDED Requirements

### Requirement: CLI command set and help are complete and discoverable

The system SHALL expose a Typer application `chronotrace` with commands `init`, `up`, `down`, `status`, `add`, `chat`, `report`, and `version`, each with descriptive help text, argument validation, and `--help` output listing required arguments, options, and examples.

#### Scenario: Top-level help lists all commands

- **WHEN** a user runs `chronotrace --help` or `chronotrace -h`
- **THEN** the output lists all eight commands with short descriptions and the exit code is 0

#### Scenario: Per-command help shows usage

- **WHEN** a user runs `chronotrace <command> --help` for any of `init`, `up`, `down`, `status`, `add`, `chat`, `report`, `version`
- **THEN** the output describes the command's purpose, its positional arguments, and its options (including `--config` and `--api-url` where applicable) and exits 0

#### Scenario: Unknown command is rejected

- **WHEN** a user runs `chronotrace unknown-cmd`
- **THEN** the CLI exits non-zero with an actionable error naming the unknown command and suggesting `--help`

### Requirement: Command implementations are separated from HTTP and Docker concerns

The system SHALL keep per-command logic under `src/chronotrace/cli/commands/`, SHALL put all HTTP request logic in a reusable `src/chronotrace/cli/api_client.py`, and SHALL put all Docker Compose process management in `src/chronotrace/cli/docker.py`, so command modules contain only argument parsing, delegation, and Rich rendering.

#### Scenario: Commands delegate through API client

- **WHEN** `add`, `chat`, `report`, or `status` need to contact the backend
- **THEN** the implementation calls the API client rather than constructing `httpx` requests inline (verifiable by import/code review and by mocking the client in tests)

#### Scenario: Up/down/status delegate through Docker helper

- **WHEN** `up`, `down`, or `status` need to inspect or control containers
- **THEN** the implementation calls the Docker helper rather than invoking `subprocess` for `docker compose` directly (verifiable by code review and by mocking the helper)

#### Scenario: No direct Neo4j access from CLI

- **WHEN** any CLI command executes
- **THEN** it does not import `neo4j` or issue Cypher queries; all graph data flows through the API

#### Scenario: No business logic embedded in commands

- **WHEN** `add`, `chat`, or `report` handle ingestion, agent, or analytics concerns
- **THEN** validation beyond URL/config shape and all scoring/explanation logic remains in the API/backend, not in CLI command functions

### Requirement: `chronotrace init` creates usable local configuration

The system SHALL provide `chronotrace init` which creates a usable local configuration file (default `./chronotrace.yml` or `~/.chronotrace/chronotrace.yml` when run outside a project), creates or verifies the configured data and repository directories, scaffolds required deployment assets, and does not overwrite existing user files without explicit confirmation.

#### Scenario: Init in empty directory creates config

- **WHEN** a user runs `chronotrace init` in a directory with no existing `chronotrace.yml`
- **THEN** the command creates the config file from `chronotrace.yml.example` or built-in defaults, creates the resolved `data_dir` and `repositories_dir`, and prints a Rich success message with the created paths

#### Scenario: Init is idempotent without overwrite

- **WHEN** `chronotrace init` is run where `chronotrace.yml` already exists and no `--force` is given
- **THEN** the command does not overwrite the file, reports that configuration already exists, and exits 0 or with an informative non-destructive status

#### Scenario: Init creates directories from configured paths

- **WHEN** the resolved configuration points `data_dir` and `repositories_dir` to non-existent paths (including `~`-prefixed paths)
- **THEN** `init` expands `~`, resolves absolute paths, creates the directories, and verifies writability

#### Scenario: Init respects explicit config path

- **WHEN** a user runs `chronotrace init --config /tmp/my.yml`
- **THEN** the file is created at `/tmp/my.yml` and subsequent commands using `--config /tmp/my.yml` load that file

### Requirement: `chronotrace up` starts the Compose stack and waits for readiness

The system SHALL provide `chronotrace up` which starts the Docker Compose stack in detached mode and waits for API readiness, reporting progress with Rich and failing with an actionable error if the stack does not become ready within the timeout.

#### Scenario: Up starts services and confirms readiness

- **WHEN** a user runs `chronotrace up` with Docker available and the API becomes healthy within the timeout
- **THEN** the command invokes the Docker helper to run `docker compose up -d`, polls the API client readiness endpoint, and prints a Rich success message indicating services are ready

#### Scenario: Up reports readiness timeout clearly

- **WHEN** the Compose stack starts but the API does not become ready before the timeout
- **THEN** the command exits non-zero with a Rich error stating the timeout elapsed, how long it waited, and suggesting `chronotrace status` and `docker compose logs` for diagnosis

#### Scenario: Up surfaces Docker errors

- **WHEN** Docker is not running or `docker compose` fails
- **THEN** the command exits non-zero with a concise actionable error (e.g., Docker not found, permission denied) without exposing stack traces by default

### Requirement: `chronotrace down` stops services without deleting volumes by default

The system SHALL provide `chronotrace down` which stops and removes Compose containers/networks without deleting named persistent volumes (e.g., `neo4j_data`) by default, preserving ingested data across restarts.

#### Scenario: Down preserves volumes by default

- **WHEN** a user runs `chronotrace down` after a successful `up`
- **THEN** containers are stopped and removed, named volumes remain, and Rich output confirms services stopped while data was preserved

#### Scenario: Down with volumes flag removes volumes when requested

- **WHEN** a user runs `chronotrace down --volumes` (or `-v`)
- **THEN** the Docker helper passes `-v`/`--volumes` to `docker compose down`, volumes are removed, and output warns that persistent data was deleted

### Requirement: `chronotrace status` distinguishes container state from API readiness

The system SHALL provide `chronotrace status` which reports both Docker container state (running/stopped/exited per service) and API readiness (reachable and healthy vs unreachable/unhealthy) so a user can distinguish "containers running" from "services ready".

#### Scenario: Status when stack is healthy

- **WHEN** containers are running and the API health probe succeeds
- **THEN** `status` renders a Rich table/panel showing each service as running and API as ready (e.g., green checkmarks)

#### Scenario: Status when containers run but API is not ready

- **WHEN** containers are running but the API health probe fails or times out
- **THEN** `status` shows containers as running and API as not ready, with a hint to check logs or wait

#### Scenario: Status when stack is stopped

- **WHEN** no Compose containers are running
- **THEN** `status` reports all services as stopped and API as unreachable

#### Scenario: Status respects overrides

- **WHEN** a user runs `chronotrace status --api-url http://custom:8000 --config /tmp/my.yml`
- **THEN** the command checks the specified API URL and loads the specified config for Compose project resolution

### Requirement: `chronotrace add` validates Git URL and starts ingestion via API

The system SHALL provide `chronotrace add <git-url>` which validates the Git URL format locally (rejecting unsupported transports/empty input) and, on success, calls the API to start ingestion, returning a job or ingestion identifier and status information.

#### Scenario: Add with valid URL starts ingestion

- **WHEN** a user runs `chronotrace add https://github.com/org/repo.git` and the API is reachable
- **THEN** the command validates the URL, POSTs to the ingestion endpoint via the API client, and renders the returned job/ingestion identifier and status with Rich

#### Scenario: Add rejects invalid URL locally

- **WHEN** a user runs `chronotrace add not-a-url` or `chronotrace add ""`
- **THEN** the command fails locally (no API call) with a concise Rich error naming the invalid URL and listing supported formats (https, http, ssh git@)

#### Scenario: Add with unreachable API fails clearly

- **WHEN** the API is unreachable during `add`
- **THEN** the command exits non-zero with a Rich error stating the API URL that was attempted and suggesting `chronotrace status` and `chronotrace up`

#### Scenario: Add validates before any Docker interaction

- **WHEN** validation fails
- **THEN** no Docker helper or graph operation is invoked

### Requirement: `chronotrace chat` sends repository-scoped questions via API

The system SHALL provide `chronotrace chat` which sends a repository-scoped question to the API and renders the evidence-grounded response, supporting non-interactive single-question mode and, where implemented, interactive REPL mode.

#### Scenario: Chat sends question and renders response

- **WHEN** a user runs `chronotrace chat --repo <id-or-url> "how is auth handled?"` (or `chronotrace chat` with prompts for repo and question) and the API is reachable
- **THEN** the command POSTs the question with repository scope via the API client and renders the returned answer (evidence, citations) with Rich markdown/panels

#### Scenario: Chat requires repository scope

- **WHEN** a user runs `chat` without specifying a repository identifier and none can be inferred from config
- **THEN** the command exits non-zero with an actionable error explaining how to provide `--repo` or select a repository

#### Scenario: Chat surfaces API errors

- **WHEN** the API returns an error (e.g., unknown repository, model unavailable)
- **THEN** the command renders a concise Rich error including the API's message and, where relevant, the repository identifier

### Requirement: `chronotrace report` requests and renders deterministic analytics

The system SHALL provide `chronotrace report` which requests deterministic analytics for a repository (and optionally a file/path filter) from the API and renders a readable Rich report including evidence, scores with breakdowns, and limitation notices where data is insufficient.

#### Scenario: Report renders analytics for a repository

- **WHEN** a user runs `chronotrace report --repo <id>` (or with `--file <path>`) and the API returns analytics
- **THEN** the command renders tables/panels for change criticality, regression risk, historical impact, and test recommendations with paths, counts, and evidence citations using Rich

#### Scenario: Report surfaces insufficient-history state

- **WHEN** the API indicates insufficient history or missing evidence for a target
- **THEN** the CLI renders the limitation explicitly rather than showing empty or zero-risk output

#### Scenario: Report respects config and API URL overrides

- **WHEN** a user runs `chronotrace report --config PATH --api-url URL --repo <id>`
- **THEN** the command uses the specified config for defaults and the specified API URL for the request

### Requirement: `chronotrace version` reports installed package version

The system SHALL provide `chronotrace version` which prints the installed ChronoTrace version from the single authoritative source, matching the value in `pyproject.toml` / `src/chronotrace/__init__.py`, and SHALL support `chronotrace --version` as an alias where idiomatic for Typer.

#### Scenario: Version prints single-source version

- **WHEN** a user runs `chronotrace version`
- **THEN** the output contains the installed version (e.g., `0.1.0`) matching `importlib.metadata.version("chronotrace")` and `chronotrace.__version__`, without duplication or hard-coded divergence

#### Scenario: Version works from arbitrary directory

- **WHEN** a user runs `chronotrace version` from a directory without config or from `/tmp`
- **THEN** the command succeeds and still prints the version

### Requirement: Rich output for progress and errors

The system SHALL use `Rich` for all user-facing progress indicators, tables, panels, and styled error output, SHALL NOT use raw `print` for primary command output except where trivially aliased, and SHALL render errors as concise, actionable, styled messages (not raw tracebacks) unless `--verbose` is set.

#### Scenario: Long-running commands show progress

- **WHEN** `up`, `add`, or `report` perform waiting or fetching
- **THEN** the terminal shows a Rich spinner or progress indicator rather than silent hanging

#### Scenario: Errors are concise and actionable

- **WHEN** any command fails due to user error (bad URL, missing file) or service error (API unreachable)
- **THEN** the error is rendered as a Rich panel or styled text naming the cause, the value or path involved, and the next step (e.g., `chronotrace status`, check config)

#### Scenario: Success output is structured and readable

- **WHEN** `status`, `report`, or `chat` succeed
- **THEN** output uses Rich tables, panels, or markdown rendering with clear headings and evidence fields, not unformatted JSON dumps

### Requirement: Path and API URL overrides with cross-directory usability

The system SHALL support explicit `--config PATH` (or `-c`) and `--api-url URL` overrides on applicable commands, SHALL resolve CLI overrides with precedence `CLI option > env var > config file > built-in default` consistent with iteration 1, and SHALL allow commands to be invoked from any working directory after `init`.

#### Scenario: Config override precedence

- **WHEN** a value is set in `chronotrace.yml`, an env var (`CHRONOTRACE_CONFIG` or equivalent), and `--config PATH`
- **THEN** `--config PATH` wins; absent the flag, env wins; absent env, file wins; otherwise defaults apply (verified via config loader integration)

#### Scenario: API URL override

- **WHEN** a user runs any API-calling command with `--api-url http://localhost:9000`
- **THEN** the API client uses that URL for all requests in that invocation, overriding config/env defaults

#### Scenario: Usable from arbitrary working directory

- **WHEN** a user runs `chronotrace status` or `chronotrace report --repo <id>` from a directory other than where `init` was run (after global init at `~/.chronotrace/`)
- **THEN** the command locates config via the standard search (`--config` → `./chronotrace.yml` → `~/.chronotrace/chronotrace.yml`) and succeeds without requiring `cd` to the init directory

### Requirement: API client and Docker helper separation and reusability

The system SHALL implement `src/chronotrace/cli/api_client.py` as a reusable `httpx`-based client and `src/chronotrace/cli/docker.py` as a Docker Compose helper, both independently testable with mocked I/O, and commands SHALL treat them as the only interfaces to HTTP and Docker.

#### Scenario: API client is reusable and mockable

- **WHEN** tests or commands need to call different API endpoints (health, ingest, chat, report)
- **THEN** they use a single `ChronoTraceClient` (or equivalent) configured by base URL, timeout, and optional API key header, with methods like `health()`, `add_repo(url)`, `chat(repo, question)`, `report(repo, file?)`, and HTTP errors mapped to typed `CliApiError` exceptions

#### Scenario: Docker helper is reusable and mockable

- **WHEN** tests or commands need to start, stop, or inspect containers
- **THEN** they use a `DockerHelper` (or equivalent) with methods like `up()`, `down(remove_volumes: bool)`, `ps() -> list[ServiceStatus]`, and subprocess errors mapped to typed `DockerError` exceptions

#### Scenario: Helpers are tested with mocks

- **WHEN** the test suite runs without Docker or a live API
- **THEN** all CLI unit tests for `api_client` and `docker` pass using mocked `httpx` and `subprocess` respectively, with no real network or container dependency

