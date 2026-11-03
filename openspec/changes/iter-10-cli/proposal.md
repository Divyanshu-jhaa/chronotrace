# Proposal: iter-10-cli — CLI Workflows and User-Facing Commands

## Why

ChronoTrace iterations 0–9 deliver the backend stack (configuration, graph, ingestion, static analysis, analytics, MCP, agent, API, Docker deployment) but provide only a stub `chronotrace version` CLI. Users have no supported way to initialize local configuration, start/stop services, check readiness, ingest repositories, or invoke chat/report workflows from the terminal. Iteration 10 completes the CLI as the primary user interface so a new user can execute the documented end-to-end workflow (`init` → `up` → `add` → `chat`/`report` → `status` → `down`) without direct API, Docker, or Neo4j interaction.

## What Changes

- Extend `src/chronotrace/cli/main.py` from a single `version` command to a full Typer command group with eight commands: `init`, `up`, `down`, `status`, `add <git-url>`, `chat`, `report`, `version` (existing enriched).
- Create `src/chronotrace/cli/commands/` package holding one module per command (or grouped where cohesive) so command functions contain only argument parsing, delegation, and Rich rendering.
- Create `src/chronotrace/cli/api_client.py` — reusable `httpx`-based API client encapsulating base URL, timeout, error mapping, and typed request/response handling for `add`, `chat`, `report`, and `status` readiness probes. CLI commands call this client; they do not build raw HTTP requests inline.
- Create `src/chronotrace/cli/docker.py` — Docker Compose helper encapsulating `docker compose` subprocess invocation (`up`, `down`, `ps`/`inspect`), readiness waiting with timeout/polling, and status mapping. CLI commands call the helper; they do not spawn Docker processes directly.
- Implement `chronotrace init` to create user configuration from `chronotrace.yml.example` (or built-in defaults), create/verify data and repository directories from resolved configuration, and scaffold deployment assets (e.g., copy `.env.example` guidance, ensure `docker-compose.yml` readiness) without overwriting existing user files without explicit confirmation.
- Implement `chronotrace up` to start the Compose stack (`neo4j`, `mcp-server`, `agent-api`) in detached mode, wait for API readiness via the API client, and render progress/errors with Rich. Supports `--config PATH` and `--api-url` overrides.
- Implement `chronotrace down` to stop services (`docker compose down`) without removing named volumes by default (add `--volumes` opt-in flag only if already established by iter-9; otherwise default preserves data and document the flag).
- Implement `chronotrace status` to report both container state (via Docker helper `ps`) and API readiness (via `GET /health` or equivalent) and clearly distinguish "containers running" from "services ready".
- Implement `chronotrace add <git-url>` to validate the Git URL format locally (supported transports) and POST to the API ingestion endpoint, returning a job/ingestion identifier and human-readable status. Errors from validation or API are rendered as concise Rich messages.
- Implement `chronotrace chat` to send a repository-scoped question to the API (`POST /chat` or equivalent), stream or receive the evidence-grounded response, and render it with Rich markdown/panels. Requires a repository identifier or URL context.
- Implement `chronotrace report` to request deterministic analytics for a repository/file target from the API and render a readable report (criticality scores, regression evidence, impact, test recommendations) with Rich tables/panels.
- Implement `chronotrace version` to report the installed package version from the single authoritative source (`importlib.metadata` / `chronotrace.__version__`).
- Ensure every command supports `--help` with useful descriptions and argument validation (missing args, invalid URL, unreachable API) producing actionable errors.
- Provide explicit `--config PATH` and `--api-url URL` overrides on commands where configuration or endpoint selection matters (`init`, `up`, `status`, `add`, `chat`, `report`); overrides follow precedence established in iteration 1 (`CLI option > env > file > defaults`).
- Make all commands usable from arbitrary working directories after `init` (resolve config via standard search: explicit `--config` → `./chronotrace.yml` → `~/.chronotrace/chronotrace.yml`).
- Use `Rich` for all progress indicators, tables, panels, and error styling; avoid raw `print` for user-facing output.

## Capabilities

### New Capabilities

- `cli`: User-facing CLI workflows and supporting infrastructure — eight Typer commands (`init`, `up`, `down`, `status`, `add`, `chat`, `report`, `version`), Rich output, reusable API client (`api_client.py`), Docker Compose helper (`docker.py`), path/API-URL overrides, and cross-directory usability.

### Modified Capabilities

- None — no prior `openspec/specs` capabilities exist for CLI behavior; `foundation` (iter-0) only covered `version` as a package entry point and is superseded in scope by this iteration's full command set without modifying its spec requirements.

## Impact

- **Code:** `src/chronotrace/cli/main.py` (expanded), `src/chronotrace/cli/api_client.py` (new), `src/chronotrace/cli/docker.py` (new), `src/chronotrace/cli/commands/` (new package: `init.py`, `up.py`, `down.py`, `status.py`, `add.py`, `chat.py`, `report.py`, `version.py` or equivalent grouping), `src/chronotrace/__init__.py` (version source).
- **APIs/Services:** No new HTTP endpoints in this iteration; CLI is a consumer of existing API routes (`POST /ingest`, `POST /chat`, `GET /report`, `GET /health`, etc. from iter-8). Docker helper consumes `docker compose` CLI as an external process dependency.
- **Dependencies:** `typer`, `rich`, `httpx`, `pyyaml` already declared in `pyproject.toml`; no new runtime dependencies. `docker` CLI is an external prerequisite (prerequisite check, not a Python dep).
- **Systems:** Local filesystem (config file creation, data/repos dirs), Docker Engine/Compose, and remote API. No direct Neo4j access from CLI; no ingestion/analytics/agent logic in CLI.
- **Tests/Tooling:** Unit tests mock `httpx` and Docker subprocesses; CLI invocation tested via `typer.testing.CliRunner`. End-to-end tests (optional) exercise real stack with Docker/Compose present.
- **Breaking changes:** None — extends CLI surface; existing `chronotrace version` behavior preserved (output format may gain Rich styling but remains compatible).
- **Security:** No secret handling beyond existing `config` loader; CLI never prints secret values, never embeds API keys in output, and delegates auth to API client headers/env.
