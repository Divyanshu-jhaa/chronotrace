# Design: Iteration 8 — FastAPI Backend and Service Lifecycle

## Context

After Iterations 1–7 ChronoTrace has: a strictly validated config object (`src/chronotrace/config/` — Pydantic v2, env-secret overlay, path expansion, no YAML re-parsing outside the loader); a Neo4j driver lifecycle with single reused driver, short-lived sessions, parameterized Cypher and repository isolation (Iteration 2); historical ingestion with `STAGE` enum and idempotent `MERGE`s (Iteration 3); AST-based import graph independent of execution (Iteration 4); deterministic analytics with `status`/`evidence`/`limitations` and decay (Iteration 5); an MCP server with five validated tools and Streamable HTTP transport (Iteration 6); and a model client plus agent tool-calling loop with iteration limits and evidence grounding (Iteration 7).

No HTTP surface exists yet. CLI (Iteration 10) needs an HTTP API to `add`/`chat`/`report`/`status`, and the web UI (Iteration 11) needs the same contracts; `chronotrace up` needs a readiness probe to replace "container started → assume ready". Iteration 8 is the only place business logic meets HTTP: it must coordinate the already-tested lower layers, not re-implement them, and must own lifespan, validation at the edge, observability of long-running ingestion, and clean teardown so that Compose restart loops and `pytest` suite shutdowns do not leak drivers.

See `proposal.md` — Why/What Changes for motivation and `specs/api/spec.md` for normative HTTP contracts; this doc covers *how*.

Constraints:
- Python 3.12+, `src` layout, `fastapi`, `uvicorn[standard]`, `pydantic`, `httpx`, `neo4j` already in `pyproject.toml`; no new external runtime dependency required.
- Secrets from environment only; `extra="forbid"` on config; error messages must not leak secrets, URIs, or Cypher.
- Repository isolation by `repository_id`; every read filtered by repository scope.
- Path safety uses canonical POSIX rules shared with ingestion/static analysis/Iteration 6 validators.
- No arbitrary Cypher exposure; routes never call `session.run`.
- Containers vs host: neo4j reached via Compose service hostname `neo4j` in containers, `localhost` from host CLI — unified via `graph.uri` from validated config.

## Goals / Non-Goals

**Goals:**
- Ship a runnable FastAPI app (`chronotrace.api.main:app`) with `lifespan` that validates config/directories/Neo4j/model, initializes shared `driver`/`analytics`/`McpClient`/`Agent`, serves typed endpoints, and tears down cleanly, runnable directly via `uvicorn` and later via the `agent-api` Compose service.
- Provide strictly typed request/response schemas and a shared error envelope so CLI/UI clients can rely on shape and codes.
- Separate `GET /health` (process liveness) from `GET /ready` (dependency health) with `200` vs `503` so `chronotrace up` can wait for readiness without false positives.
- Make ingestion observable without blocking the event loop: `POST /repositories` enqueues a background job (`JobStore`), returns `job_id` immediately, and exposes pollable `GET /repositories` + `GET /repositories/{id}`/`GET /jobs/{id}` with stage/progress/error.
- Keep `POST /chat` repo-scoped and evidence-grounded via agent delegation, and `POST /reports` deterministic without LLM, delegating to analytics.
- Keep every route thin; business logic stays in `core/*`, `agent/`, `mcp`.
- Avoid event-loop blocking, bound all inputs, redact secrets, and close all drivers/clients on shutdown.

**Non-Goals:**
- Docker image and `docker-compose.yml` `agent-api`/`mcp-server` services, volume mounts, and restart policies (Iteration 9) — design notes intended wiring but does not change `docker-compose.yml` beyond readiness notes.
- CLI command implementations `chronotrace add`/`chat`/`report`/`status`/`up`/`down` and their `api_client.py`/`docker.py` helpers (Iteration 10) — this iteration provides the server they will call, verified via throwaway `httpx` client.
- Web UI repository selection, chat/report views, graph visualization (Iteration 11).
- Authentication/authorization, rate limiting, WebSocket/SSE push, JWT — local trust boundary with bounded-input validation only in MVP.
- Hot-reload of `chronotrace.yml`, migrations/data-model changes (no new node/relationship types), real coverage, embeddings, SZZ, backtesting (Iteration 12 / PLAN §7).

## Decisions

### Decision 1: FastAPI with async `lifespan` owning all shared singletons

**Choice:**
```python
# src/chronotrace/api/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from chronotrace.config import load_config, validate_startup
from chronotrace.api.dependencies import AppState


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = AppState()
    config = load_config()  # only once
    state.config = config
    # 1) startup checks (fail fast)
    validate_startup(config)  # secrets, filesystem
    # 2) graph
    state.driver = create_neo4j_driver(
        config.graph.uri, auth=(env.NEO4J_USERNAME, env.NEO4J_PASSWORD)
    )
    await to_thread(state.driver.verify_connectivity)
    state.graph_gateway = GraphGateway(state.driver, database=config.graph.database)
    state.analytics = AnalyticsService(state.graph_gateway, config)
    # 3) mcp + agent
    state.mcp_client = McpClient(config.mcp.url if hasattr(config, "mcp") else default_url)
    await state.mcp_client.connect()
    state.agent = Agent(config.agent, state.mcp_client, config)
    state.job_store = JobStore(max_jobs=1000)
    app.state.app_state = state
    yield
    # shutdown: drain, then close
    await state.job_store.cancel_all(reason="shutdown")
    await state.mcp_client.aclose()
    state.driver.close()


app = FastAPI(lifespan=lifespan, title="ChronoTrace API", version=__version__)
```

Dependencies in `dependencies.py` expose `get_app_state()`, `get_config()`, `get_driver()` via `Depends`, so routes receive state without importing globals. `load_config` is called only in lifespan, never in routes.

**Rationale:** Single startup validation matches Iteration 1's "validate before services become ready" and Iteration 5's readiness gate. Singleton driver mirrors Iteration 2's "reuse, don't create per query" and lets `JobStore` background work reuse it via short-lived sessions.

**Alternatives considered:**
- `on_event("startup")` / `on_event("shutdown")` — rejected: deprecated in FastAPI docs, less composable with `app.state`.
- Per-request `load_config()` in every route — rejected: re-parses YAML/env, breaks single-source validation and makes testing require filesystem.
- Separate lifespan per route module — rejected: one app, one lifecycle.

### Decision 2: Module layout `src/chronotrace/api/` — thin routes, typed schemas, isolated job tracking

**Choice:**
```
src/chronotrace/api/
  __init__.py
  main.py              # app + lifespan, include_router
  dependencies.py      # get_app_state, get_config, get_graph_gateway, get_agent, get_job_store
  jobs.py              # Job, JobStatus, JobStage, JobStore (async-safe, bounded)
  schemas/
    __init__.py
    health.py          # HealthResponse, ReadyResponse, ReadyChecks
    repositories.py    # RepositoryRegisterRequest, RepositoryResponse, RepositoryListResponse
    jobs.py            # JobResponse, JobListResponse
    chat.py            # ChatRequest, ChatResponse, ChatEvidence
    reports.py         # ReportRequest, ReportResponse (wraps analytics contract)
    errors.py          # ErrorEnvelope, ErrorDetail, error codes
  routes/
    __init__.py
    health.py          # GET /health, GET /ready
    repositories.py    # POST /repositories, GET /repositories, GET /repositories/{id}, GET /jobs/{id}
    chat.py            # POST /chat
    reports.py         # POST /reports (and/or GET /reports)
  services/            # optional thin orchestrators if routes need helper
    repository_service.py
    report_service.py
```

**Rationale:** Aligns with PLAN §4 (`src/chronotrace/api/main.py`, `dependencies.py`, `schemas/`, `routes/`) and keeps route files focused. Schemas are the only place response shapes are defined; services are optional and stay thin.

**Alternatives considered:**
- Single `routes.py` with all endpoints — rejected: mixes health, ingestion, chat, report concerns; hard to test per-endpoint.
- Business logic in routes — rejected: violates dependency direction (`API coordinates, not duplicates`) and duplicates scoring/ingestion logic.

### Decision 3: Strict schemas and shared error envelope via exception handlers

**Choice:** Each schema is `pydantic.BaseModel` with `model_config = ConfigDict(extra="forbid")` where strictness is desired (so unknown fields → 422). Git URL validated by `Annotated[str, AfterValidator(validate_git_url)]` (http/https `://` or `git@`/`ssh://`, length 1–2048, no `\x00`, no shell interpolation — the value is treated as data, passed as `str` to `git clone` via `subprocess` list form, never via shell). `file_path` uses `AfterValidator(validate_file_path)` reusing Iteration 6's canonical POSIX validator (reject absolute, `..`, backslash, `\x00`).

Error envelope handler:
```python
@app.exception_handler(RequestValidationError)
async def validation_handler(req, exc):
    return JSONResponse(
        422,
        {"error": {"code": "validation_error", "message": humanize(exc), "details": exc.errors()}},
    )


@app.exception_handler(HTTPException)
async def http_handler(req, exc):
    return JSONResponse(
        exc.status_code, {"error": {"code": code_for(exc.status_code), "message": exc.detail}}
    )
```
Redaction helper strips any value whose key contains `api_key|password|secret|token|auth|uri`.

**Rationale:** Typed schemas make malformed CLI/UI requests fail early with actionable `422`; a single envelope keeps client error handling uniform across 7 endpoints.

**Alternatives considered:**
- Per-route `try/except` building dict errors — rejected: inconsistent codes, missed `RequestValidationError` from FastAPI itself.
- OpenAPI-only docs without runtime `extra="forbid"` — rejected: silent acceptance of typos.

### Decision 4: Health vs readiness as separate routes with correct status codes

**Choice:**
- `GET /health` → `{"status":"ok","version":__version__,"uptime_seconds":...}` — 200 always when process is up; no `driver.verify_connectivity`.
- `GET /ready` → probes `state.driver.verify_connectivity()` (or cached last probe with short TTL e.g., 5s), `config` validated flag, and `agent` availability flag. Returns:
  - `200 {"status":"ready","checks":{"neo4j":"ok","config":"ok","agent":"ok"}}` when all hard checks pass.
  - `503 {"status":"not_ready","checks":{"neo4j":"unavailable","reason":"..."}}` otherwise.
`chronotrace up` and Compose `healthcheck: test: ["CMD","curl","-f","http://localhost:8000/ready"]` wait for `200 ready`, not `health`.

**Rationale:** Distinct probes prevent the Iteration 2 gate violation ("report ready while DB is down"). Uptime probes stay cheap; readiness probes are the integration point for orchestration.

**Alternatives considered:**
- Single `/health` that checks dependencies — rejected: kills process liveness vs readiness distinction, causes premature restarts.
- `200` always with `status: degraded` — rejected: orchestrators treat any `200` as ready; `503` is the standard signal.

### Decision 5: Ingestion job model and `JobStore` — polling, bounded, async-safe

**Choice:**
```python
class JobStatus(str, Enum): queued="queued"; running="running"; completed="completed"; failed="failed"
class JobStage(str, Enum): CLONING="CLONING"; EXTRACTING="EXTRACTING"; FILTERING="FILTERING"; WRITING_GRAPH="WRITING_GRAPH"; COMPLETED="COMPLETED"; FAILED="FAILED"
@dataclass
class Job: job_id: UUID; repository_id: str; status: JobStatus; stage: JobStage; progress: float|None; error: ErrorDetail|None; created_at: datetime; updated_at: datetime; repository_url: str

class JobStore:
    def __init__(self, max_jobs=1000): self._jobs: dict[UUID, Job]={}; self._lock=asyncio.Lock(); self.max_jobs=max_jobs
    async def create(...) -> Job: ...
    async def update(job_id, status=None, stage=None, progress=None, error=None) -> Job: ...
    async def get(job_id) -> Job|None: ...
    async def get_by_repo(repo_id) -> Job|None: ...  # latest job for repo
    async def list_all() -> list[Job]: ...
```

`POST /repositories` handler:
```python
job = await job_store.create(repo_id=repo_id, url=url)
# immediately return
background_tasks.add_task(run_ingestion, job.job_id, repo_id, url, branch)
return JSONResponse(
    201, {"repository_id": repo_id, "job_id": str(job.job_id), "status": job.status}
)
```
`run_ingestion` runs via `background_tasks` (FastAPI `BackgroundTasks`) or `asyncio.create_task` wrapping `await asyncio.to_thread(ingestion_service.ingest, ...)`. It updates `JobStore` stage by stage via callbacks (`on_stage(stage, progress)`). On success it `update(job_id, status=completed, stage=COMPLETED)`; on exception it `update(status=failed, stage=FAILED, error=...)` without advancing repository head/timestamp.

Boundedness: `max_jobs` evicts oldest `completed`/`failed` beyond limit; `queued`/`running` are never evicted. List endpoints are bounded (`page_size` default 20, max 100) but jobs themselves are not paginated beyond store limit in MVP.

**Rationale:** Polling is sufficient for CLI/UI without WebSocket complexity. `asyncio.Lock` (or `threading.Lock` if accessed from threads) gives safe concurrent create/read from multiple request handlers.

**Alternatives considered:**
- Synchronous ingestion in the request thread — rejected: blocks loop, violates spec's non-blocking requirement.
- Celery/RQ/Arq queue — rejected: extra dependency/Redis for MVP; `BackgroundTasks` suffices.
- WebSocket push — rejected: deferred to post-MVP; polling keeps Iteration 10 CLI simpler.

### Decision 6: Non-blocking guarantee — never run heavy work on the event loop

**Choice:** Every ingestion stage that does I/O or CPU work (`git clone/fetch`, `pydriller` commit walk, file filtering, `MERGE` batch writes, AST parsing) is invoked via `await asyncio.to_thread(...)` inside the background task, or the whole ingestion function is `def ingest(...)` run in a thread. Route handlers themselves are `async def` but contain only validation + delegation (no sync sleep/blocking read). The spec's "return within 500 ms" is verified by a unit test mocking `ingestion_service.ingest` to `time.sleep(5)` inside `to_thread` and asserting `POST /repositories` returns in < 500 ms.

**Rationale:** Satisfies the "Avoid blocking the event loop" non-negotiable and keeps `GET /health`/`POST /reports` responsive during ingestion.

**Alternatives considered:**
- `asyncio.create_task` running an `async def ingest_async` that still calls blocking Git APIs — rejected: still blocks unless every blocking call is wrapped.
- `ProcessPoolExecutor` — rejected: heavier, not needed for I/O-bound ingestion.

### Decision 7: Chat delegates to agent; report delegates to analytics (distinct paths)

**Choice:**
- `POST /chat` handler: validate `repository_id` (exists via `graph_gateway.repository_exists`), validate `question` (str, 1–10000 chars), then `result = await agent.ask(repository_id=repo_id, question=q, conversation_id=cid)` — `Agent` does the tool loop via `McpClient`; handler wraps `ModelUnavailable|Timeout|McpError` into `503`/`504` with retry hint, never fabricates text. Returns `{"answer": result.text, "citations": result.citations, "evidence": result.evidence, "limitations": result.limitations, "conversation_id": cid or generated}`.
- `POST /reports` handler: validate `repository_id` + `file_path` (POSIX traversal check), then `result = analytics_service.get_change_criticality(repo_id, path)` (or `report_service.build(report, repo_id, path)` that may combine multiple analytics calls). No `McpClient` or model involvement. Returns the analytics contract verbatim under HTTP 200 with `status` in body (`ok|insufficient_history|not_found`).

**Rationale:** Chat is LLM-dependent (correct to fail closed on model outage); reports must stay available without LLM, satisfying the spec's "Report generation can work without an LLM." Thin handlers keep the evidence-first guarantee in the agent layer, not re-implemented at HTTP.

**Alternatives considered:**
- `POST /reports` going through chat/agent — rejected: couples deterministic reports to model availability and cost.
- `GET /reports?repository_id=&file=` only — rejected: long `file_path` with `?` encoding is fragile; allow both, but spec mandates POST shape so we support `POST`.

### Decision 8: Dependency wiring and test seams via `dependencies.py`

**Choice:** `dependencies.py` exports factories test can override:
```python
def get_app_state(request: Request) -> AppState:
    return request.app.state.app_state


def get_config(state=Depends(get_app_state)) -> ChronoTraceConfig:
    return state.config


def get_job_store(state=Depends(get_app_state)) -> JobStore:
    return state.job_store
```
Tests override via `app.dependency_overrides[get_job_store] = lambda: fake_store` or by replacing `app.state.app_state`. All heavy dependencies are injected, so `tests/unit/api` can run without Neo4j/model.

**Rationale:** Makes `lifespan` startup failures, readiness `503`, job transitions, and chat/report validation testable without a live DB/provider.

**Alternatives considered:**
- Global module-level singletons imported directly in routes — rejected: pollutes import-time side effects, hard to mock.
- `dependency_injector` library — rejected: extra dep; FastAPI's `Depends` suffices.

### Decision 9: Config reuse and optional `api` config keys

**Choice:** `main.lifespan` calls `load_config()` once; routes never call it again. If `api.host`/`api.port` are needed, they are added to `config/models.py` as:
```python
class ApiConfig(BaseModel, frozen=True, extra="forbid"):
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)


class ChronoTraceConfig(...):
    api: ApiConfig = Field(default_factory=ApiConfig)
```
Validation rejects unknown keys; Docker/Compose overrides via `CHRONOTRACE_API_HOST/PORT` env if wired via `loader`. No secret handling change.

**Rationale:** Keeps Iteration 1's `extra="forbid"` and single-loader invariant; adding `api` keys is additive and backward compatible (defaults fill missing `api` section).

**Alternatives considered:**
- `uvicorn` host/port via CLI args only — rejected: validated config gives documented precedence and healthcheck consistency.
- Reading `API_HOST` env directly in `main.py` — rejected: splits secret/non-secret env handling from loader.

### Decision 10: Security enforcement at the HTTP boundary

**Choice:** Three layers:
1. Pydantic validators on every input (`url`, `file_path`, `question`, `repository_id`) bound lengths, reject `\x00`, traversal, and absolute paths.
2. Global `RequestValidationError` handler returning `validation_error` without echoing raw payload beyond field name.
3. Redaction filter on any error string that matches secret key names before returning JSON; logs use `logging` formatter that redacts similarly.

Rate/payload: FastAPI `limit` via `uvicorn --limit` or Starlette `BaseHTTPMiddleware` that rejects bodies > 1 MB (configurable) with `413`.

**Rationale:** Mirrors Iteration 6's input validation boundary at the MCP layer, now at HTTP; keeps the non-negotiable "Secrets must not be committed or stored in YAML" and "Do not print secret values in logs or exceptions."

**Alternatives considered:**
- No body-size limiting — rejected: unbounded `question` or JSON could force large agent context.

## Risks / Trade-offs

- **Neo4j not yet ready when readiness probes start** → Mitigation: `/ready` probes with short TTL cache plus retry; `chronotrace up` waits with backoff (future Iteration 10). Lifespan startup that depends on Neo4j retries once with short delay before failing; `JobStore` jobs not yet started stay `queued`.
- **Ingestion background task failure excepts silently** → Mitigation: `run_ingestion` wraps body in `try/except` that `job_store.update(status=failed, error=...)` and never re-raises; `BackgroundTasks` docs warn exceptions are silent, so the explicit catch is mandatory.
- **Event-loop blocking slipped in via sync `graph` or `git` calls** → Mitigation: `asyncio.to_thread` for every sync heavy call; code review checklist checks for `sleep`/`subprocess.run`/`pydriller` on the main thread; load test asserts `/health` latency < 50 ms while ingestion runs.
- **JobStore unbounded growth** → Mitigation: `max_jobs` with eviction of oldest `completed`/`failed`; `GET /repositories` pagination caps; iteration tests assert `len(store) <= max`.
- **Chat model timeout vs. FastAPI timeout mismatch** → Mitigation: agent timeout (e.g., 30s) shorter than uvicorn proxy timeout (60s); on timeout return `504` with retry hint. Iteration limits (Iteration 7 max iterations) prevent endless tool loops.
- **Report endpoint confusion between `POST` JSON and `GET` query** → Mitigation: support both shapes internally (shared validator), document `POST /reports` as canonical; tests cover both.
- **Startup ordering vs Docker Compose `depends_on`** → Mitigation: Compose `healthcheck` against `/ready`, not `/health`; API `depends_on: neo4j: condition: service_healthy` (neo4j healthcheck uses `cypher-shell`). Still, application retries readiness probe rather than assuming `depends_on` means ready.
- **Error envelope vs FastAPI's default 422 HTML for some clients** → Mitigation: exception handlers cover `RequestValidationError`, `StarletteHTTPException`, and generic `Exception` → `500` envelope, so no HTML path remains.
- **Secrets in `graph.uri` if it contains `user:pass@host`** → Mitigation: `graph.uri` is `bolt://host:7687` without credentials; `auth` comes from env tuple; redaction still strips `password` substrings if a bad URI is supplied.
- **Multiple workers (uvicorn `--workers >1`) share no JobStore** → Mitigation: document `workers=1` for MVP (JobStore is in-memory). Iteration 9 may note this; future persistence to Neo4j or Redis would be needed for multi-worker.

## Migration Plan

No schema or data migration required in this iteration — the API is stateless aside from in-memory jobs; graph data unchanged.

1. Add/verify `fastapi` + `uvicorn[standard]` in `pyproject.toml` (already present) and run `pip install -e ".[dev]"`.
2. Land `src/chronotrace/api/` (main, dependencies, schemas, routes, jobs) without changing existing CLI/graph/ingestion/analytics/mcp/agent behavior; all prior tests remain green.
3. Run a local Neo4j (`docker compose up neo4j` or `neo4j:7687` elsewhere) with an existing fixture graph; start the API via `uvicorn chronotrace.api.main:app --host 127.0.0.1 --port 8000 --reload` and verify `curl -f http://localhost:8000/health` → 200 and `curl -f http://localhost:8000/ready` → 200 (or 503 while Neo4j warming).
4. Smoke `httpx` client script: `POST /repositories` with a small public Python repo URL → poll `GET /repositories/{id}` until `completed`; `POST /reports` for a known `file_path` → assert contract; `POST /chat` with a stubbed agent/model (or real `MODEL_API_KEY`) → assert answer shape. This is the completion gate.
5. Rollback: stop uvicorn (`Ctrl-C` or `SIGTERM`) triggers lifespan shutdown; no persisted state to roll back. Reverting the package restores prior behavior. Dockerfile/Compose wiring in Iteration 9 does not retroactively break Iteration 8's `uvicorn` invocation.
6. Forward compatibility: Iteration 9 mounts the same image with `uvicorn chronotrace.api.main:app --host 0.0.0.0 --port 8000`; Iteration 10's `src/chronotrace/cli/api_client.py` calls these endpoints via `httpx` with the same schemas; Iteration 12's integration suite replaces the `httpx` smoke script with `pytest` API tests.

## Open Questions

- **Endpoint path prefix (`/api` vs bare):** Should routes be `/api/repositories` vs `/repositories`? The FastAPI router can be mounted at either prefix without behavior change; CLIs can handle both. Decision: ship bare paths (`/health`, `/ready`, `/repositories`, `/chat`, `/reports`) and optionally include an `/api` alias router so `cli/api_client.py` works regardless; document the bare set as canonical.
- **Job persistence across restarts:** Should `JobStore` be in-memory only (MVP) or persisted to Neo4j/a file so jobs survive API restart? In-memory satisfies the polling requirement and keeps teardown simple; persistence is deferred — if a restart loses `running` → re-query Neo4j `Repository.last_successful_ingestion` to re-derive status.
- **Pagination and filtering for `GET /repositories`:** Should listing support `?status=completed` or cursor pagination now? MVP bounds with `limit`/`offset` defaults (20/100) suffices for demo; filtering is additive later.
- **Chat streaming:** Should `POST /chat` support SSE/streamed tokens? The agent currently returns a final answer after the tool loop; streaming the final answer is a display concern deferred to web UI (Iteration 11) and does not affect the spec's non-streaming shape.
