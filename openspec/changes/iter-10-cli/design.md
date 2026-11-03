# Design: iter-10-cli — CLI Workflows and User-Facing Commands

## Context

Iteration 0 delivered `src/chronotrace/cli/main.py` with a single `version` command (`app = typer.Typer(name="chronotrace")`). Iteration 1 added `chronotrace.yml` schema v1 and environment-secret handling (Pydantic loader with precedence `CLI > env > file > defaults`). Iterations 2–9 layered Neo4j/graph, ingestion, static analysis, analytics, MCP, agent, API, and Docker Compose (`neo4j`, `mcp-server`, `agent-api` services). There is no supported terminal workflow for init/up/down/status/add/chat/report — users must call Docker and HTTP directly. The CLI currently contains no `api_client`, `docker` helper, or `commands/` package.

Constraints: Typer + Rich already in `pyproject.toml` (`typer`, `rich`, `httpx`, `pyyaml` available). CLI must not contain analytics/Cypher/agent logic (PLAN §3.2 dependency direction). Config loading must reuse `chronotrace.config.load_config` (iter-1). Docker is an external prerequisite (`docker compose` CLI), not a Python dependency. Must work on Windows/macOS/Linux. See `proposal.md` — Why.

## Goals / Non-Goals

**Goals:**

- Provide eight Typer commands with Rich output that together realize `init → up → add → chat/report → status → down`.
- Factor HTTP into `api_client.py` and Compose orchestration into `docker.py`; keep `commands/` thin and testable.
- Enforce `--config`/`--api-url` overrides with precedence consistent with iter-1 and cross-directory usability.
- Wait-for-readiness in `up` and clear distinction of container-state vs. API-ready in `status`.
- Preserve persistent volumes by default in `down`.

**Non-Goals:**

- No new HTTP endpoints or API contract changes (consumer only; endpoints defined in iter-8).
- No direct Neo4j/Cypher ingestion/analytics/agent logic in CLI (those live in graph/ingestion/analytics/agent).
- No interactive TUI beyond `chat`'s optional REPL; no web UI (iter-11).
- No encrypted secret storage or vault integration; reuse iter-1 env-only secrets.
- No background daemon or file-watching of `chronotrace.yml`.

## Decisions

### Decision 1: Typer command group with `commands/` package, thin handlers

`src/chronotrace/cli/main.py` becomes a Typer app that registers commands from `src/chronotrace/cli/commands/{init,up,down,status,add,chat,report,version}.py`. Each command function does: (a) parse/validate args via Typer, (b) resolve config/API URL, (c) delegate to `api_client` or `docker` helper, (d) render with `rich.console.Console` / `rich.table.Table` / `rich.panel.Panel` / `rich.progress`.

- Alternatives considered: Single `main.py` with all callbacks; Click instead of Typer. Rejected: single file would exceed 600 LOC and violate separation; Click would undo iter-0's Typer choice and rewrite help/exit semantics.
- Risks mitigated: Testability — `typer.testing.CliRunner` can invoke each command in isolation; logic extraction keeps helpers mockable.

### Decision 2: `api_client.py` — single httpx-based reusable client

```
class ChronoTraceClient:
  def __init__(self, base_url: str, timeout: float = 30.0, api_key: str | None = None)
  def health(self) -> HealthStatus        # GET /health or /api/health
  def add_repository(self, git_url: str) -> AddResult   # POST /repositories or /ingest
  def chat(self, repo: str, question: str) -> ChatResult  # POST /chat
  def report(self, repo: str, file: str | None) -> ReportResult  # GET /report?repo=&file=
  def close(self) / __enter__/__exit__
```

Internally wraps `httpx.Client` (sync) with optional `httpx.AsyncClient` only if any command streams. Errors from `httpx` mapped to `CliApiError(status_code, message, detail)` with concise message for Rich rendering. Base URL resolved as: `--api-url` → `CHRONOTRACE_API_URL` env → `api.base_url` from config (if present) → default `http://localhost:8000`. Timeout configurable via `--timeout` or config.

- Alternatives: `requests` library; inline `httpx` calls per command. Rejected: `requests` is not in `pyproject.toml`, `httpx` already required for API; inline calls duplicate timeout/header/error mapping and resist mocking.
- Risks mitigated: Mocking — tests patch `httpx.Client.request` or the client methods; separation ensures no Neo4j import.

Example wiring: `commands/add.py` calls `client.add_repository(git_url)`; `commands/status.py` calls `client.health()` alongside `docker.ps()`.

### Decision 3: `docker.py` — Compose helper around `docker compose` subprocess

```
class DockerHelper:
  def __init__(self, compose_file: Path | None = None, project_name: str | None = None)
  def up(self, detach: bool = True, timeout: float = 120) -> None
  def down(self, remove_volumes: bool = False) -> None
  def ps(self) -> list[ServiceStatus]   # parses `docker compose ps --format json`
  def is_available(self) -> bool        # probes `docker info` / `docker compose version`
```

Implementation uses `subprocess.run([...], capture_output=True, text=True)` with explicit `compose_file` (`--file`) and `project_name` when supplied via config (`project.name`). `up(wait: bool)` loops polling `client.health()` or `docker compose ps --filter status=running` until ready/timeout, emitting Rich spinner. `down` runs `docker compose down` or `docker compose down -v` only when `remove_volumes=True`. All compose invocations honor `--config` resolution for project directory.

- Alternatives: Docker SDK (`docker` Python package); shell script wrapper. Rejected: SDK adds dependency and diverges from Compose semantics; shell scripts are not testable via `CliRunner` and fail cross-platform.
- Risks mitigated: Missing Docker — `is_available()` check produces actionable error (`Docker not found — install Docker Desktop / Colima`); non-zero `subprocess` mapped to `DockerError(cmd, returncode, stderr)`.

### Decision 4: Config and override resolution per iter-1 precedence

`main.py` declares a shared Typer callback/option pattern:

```python
@app.callback()
def _root(
    config: Path | None = Option(None, "--config", "-c", help="Path to chronotrace.yml"),
    api_url: str | None = Option(None, "--api-url", help="Override API base URL"),
    verbose: bool = Option(False, "--verbose", help="Show tracebacks"),
):
    ctx.obj = {"config": config, "api_url": api_url, "verbose": verbose}
```

Each command resolves config via `load_config(config_path=ctx.obj["config"])` (iter-1 loader: explicit → `./chronotrace.yml` → `~/.chronotrace/chronotrace.yml`). `api_url` resolved inside `api_client` factory `make_client(ctx.obj)` using the precedence chain above. Errors from `load_config` (missing file, malformed YAML, invalid values) are caught and rendered as Rich panels with field/path guidance and without secret leakage; exit code 2 for usage errors, 1 for runtime failures.

- Alternatives: Per-command duplicate `--config` definitions; env-only API URL. Rejected: duplication drifts; env-only removes explicit CLI override user expects and breaks CI scenarios.
- Risks mitigated: Cross-directory use — path expansion (`Path.expanduser().resolve()`) and Compose project directory derived from config `project.repositories_dir` parent, so `up/status` work from `/tmp`.

### Decision 5: `init` scaffolding without destructive overwrite

`commands/init.py`:

1. Resolve target config path (explicit `--config` or `./chronotrace.yml` if cwd is project-like, else `~/.chronotrace/chronotrace.yml`).
2. If exists and not `--force`, print panel "already exists" and exit 0.
3. Otherwise copy `chronotrace.yml.example` (resolved relative to package resources or repo root) or emit defaults; write file.
4. Call `load_config(target)` to expand and create `data_dir`/`repositories_dir` (mkdir + writability probe per iter-1 validators).
5. Copy `.env.example` guidance note if `.env` absent (do not overwrite `.env`).
6. Print Rich success with paths and next step `chronotrace up`.

- Alternatives: Always overwrite; interactive prompt for every file. Rejected: always-overwrite destroys user edits; prompt-per-file complicates automation.
- Risks mitigated: Idempotency tested by running `init` twice.

### Decision 6: Rich output conventions, no raw	tracebacks

All commands instantiate `console = Console(stderr=True)` for diagnostics and `Console()` for normal output. Patterns: `with console.status("Waiting for API...")` for `up`; `Table(show_header=True, box=box.ROUNDED)` for `status`/`report`; `Panel(Markdown(...))` for `chat`; `console.print("[red]Error:[/] ...\n[dim]hint: chronotrace status[/]")` for failures. Stack traces only when `--verbose` or `CHRONOTRACE_VERBOSE=1`. JSON mode (`--json`) deferred to iter-12 — not in scope; `report` Rich rendering is the MVP.

- Alternatives: Plain `print` + `argparse`; `click` + manual ANSI. Rejected: loses Typer+Rich integration and PLAN's explicit Rich requirement.

### Decision 7: `status` dual-source rendering

`status` calls both `docker.ps()` and `client.health()` (with short timeout, default 5s). It builds a `Table` with columns `Service | Container State | API`. Container states derived from `docker compose ps --format json` fields (`State`, `Status`, `Health`). API ready is `health().ok`. If Docker unavailable, still report API state; if API unreachable, still report container state. Each observation is labeled so "running ≠ ready" is legible.

### Decision 8: `add`, `chat`, `report` validation and error mapping

- `add`: Local URL regex/parse before network: accept `https://`, `http://`, `ssh://`, `git@host:path`, `git+https://`; reject empty, bare word, `ftp://`. On local fail, exit 2 with `invalid Git URL '<val>' — expected https://... or git@...`. On API fail, exit 1 with `API error (<code>): <message> — tried <url>`.
- `chat`: If `--repo` not supplied, try `load_config`'s `project.name` as default; if still None, exit 2 prompting for `--repo`. Streamed responses (if API streams SSE) accumulated before Rich markdown render in MVP; true incremental streaming deferred.
- `report`: Accepts `--repo` required, `--file` optional; validates file path not empty when supplied; renders limitation banners when API flags `insufficient_history: true`.

## Risks / Trade-offs

- [Docker not installed or daemon not running] → Mitigation: `is_available()` pre-check with actionable message; `up`/`status` still render partial results; documented prerequisite.
- [API health endpoint path differs from assumed `/health`] → Mitigation: client probes `/health` then fallback to `/api/health` then root `/`; iter-8's actual route is the source of truth — adapt at impl time; health response typed as `{"status": "ok" | "degraded"}` with tolerance for shape variation.
- [`docker compose ps --format json` output varies by Compose version] → Mitigation: handle both JSON-lines and JSON-array outputs; unit tests cover both fixture shapes.
- [Rich rendering in non-TTY / CI] → Mitigation: `Console(force_terminal=..., no_color=...)` auto-detection; `--no-color` flag optional deferred; tests use `CliRunner(mix_stderr=False)`.
- [Config path ambiguity across cwd after init] → Mitigation: explicit `--config` honored first; search order documented in `--help` and init success message; iter-1 `find_config_file` reused.
- [chat/report large output truncation] → Mitigation: Rich pagination (`console.pager`) only on interactive terminals; non-interactive prints full output.
- [Subprocess injection via git-url or compose_file paths with spaces/shell specials] → Mitigation: `subprocess.run([...])` list form only, never `shell=True`; args shell-escaped only for error messages.
- [Version source divergence] → Mitigation: `version` command reads `importlib.metadata.version("chronotrace")` with fallback to `chronotrace.__version__`; tests assert equality.

## Migration Plan

- No data migration. Existing installs: `pip install -e .` already satisfies deps; no config schema change (iter-1 config unchanged). New users: `pip install -e .` → `chronotrace init` → `chronotrace up` → `chronotrace add <url>` → `chronotrace chat/report/status/down`.
- Existing callers of `chronotrace --help` / `chronotrace version` remain compatible (new commands additive; `--help` text expanded).
- Rollback: revert CLI package to iter-0 stub; API/Docker unaffected. No database migration.
- Configuration note: `docker-compose.yml` project name (`name:` field if present) should match `project.name` for predictable `ps` filtering; noted in init success hint.

## Open Questions

- Exact API route paths (`POST /repositories` vs `/ingest` vs `/add`, `GET /report` query shape, `POST /chat` SSE vs unary) — defer to iter-8's actual `api/routes` implementation; spec already covers behavior regardless of path, and client will adapt with minimal surface change.
- Whether `init --force` should also reset `.env` — deferred; current `init` never overwrites `.env`, only creates if absent, which is safer for MVP.
- Streaming token-by-token for `chat` — MVP accumulates full response; true streaming deferred to UI iteration if model streaming is not yet stable.
