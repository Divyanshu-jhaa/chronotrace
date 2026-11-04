# ChronoTrace API

FastAPI service at `src/chronotrace/api/main.py` (`chronotrace.api.main:app`). Single `lifespan` owns validated config, Neo4j driver, `GraphGateway`, `AnalyticsService`, `McpClient`, `Agent`, and bounded `JobStore`; clean shutdown drains jobs and closes drivers.

## Endpoints

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `GET` | `/health` | Process liveness — no dependency checks | none |
| `GET` | `/ready` | Dependency readiness (`neo4j`, `config`, `agent`) | none |
| `POST` | `/repositories` | Validate Git URL, create `repository_id` + ingestion job, trigger ingestion without blocking, `201` | none |
| `GET` | `/repositories` | List known repositories, bounded pagination | none |
| `GET` | `/repositories/{repository_id}` | Repository + latest job status (`status`, `stage`, `progress`, `error`, `created_at`/`updated_at`/`finished_at`) | none |
| `GET` | `/jobs/{job_id}` | Same job envelope by `job_id` | none |
| `POST` | `/reports` | Deterministic analytics for `{repository_id, file_path}` → common contract `{status, repository_id, target, result, evidence, limitations}`; `GET /reports?repository_id=&file_path=` alias supported | none |
| `POST` | `/chat` | Repository-scoped agent `ask` → `{answer, citations, evidence, limitations, conversation_id}` | `MODEL_API_KEY` via agent |

`GET /openapi.json` and `GET /docs` are auto-generated; `POST /reports` is canonical (GET alias for convenience).

## Schemas

### Health
```json
HealthResponse { status: "ok", version: "0.1.0", uptime_seconds: 123.4, started_at: "2024-03-15T12:00:00Z" }
ReadyResponse  { status: "ready"|"not_ready", checks: { neo4j: "ok"|"unavailable", config: "ok"|"error", agent: "ok"|"unavailable", reason?: string } }
```

### Repositories
```json
RepositoryRegisterRequest { url: string (http/https/ssh/git@/file, 1..2048, no NUL, no shell interpolation $() `` ;|), branch?: string }
RepositoryResponse       { repository_id: string, job_id: UUID, status: "queued"|"running" }  // 201, Location + X-Job-Id
RepositoryListResponse   { repositories: [{repository_id, url, ingestion_status, last_ingested_at, head}], total, limit, offset }
RepositoryStatusResponse { repository_id, url, job: { job_id, repository_id, status, stage, progress?, error?, created_at, updated_at, finished_at? }, head?, last_successful_ingestion? }  // or 404 {error:{code:"not_found"}}
JobResponse              { job_id, repository_id, repository_url, status, stage, progress?, error?, created_at, updated_at, finished_at? }
```

### Reports
```json
ReportRequest  { repository_id: string (1..256), file_path: string (POSIX repo-relative, 1..4096, no absolute, no "..", no backslash, no NUL, no %2e%2f%5c encoded traversal) }
ReportResponse { status: "ok"|"insufficient_history"|"not_found", repository_id, target, result, evidence, limitations } // 200 for ok/insufficient_history, 404 for not_found, 422 for validation, 503 for graph_unavailable
```

### Chat
```json
ChatRequest  { repository_id: string (1..256), question: string (1..10000), conversation_id?: string }
ChatResponse { answer: string, citations: string[], evidence: object, limitations: string, conversation_id: string } // 200; 422 validation, 404 not_found, 503 model_unavailable/mcp_unavailable/graph_unavailable, 504 timeout
```

All schemas use `extra="forbid"` — unknown fields → `422 {error:{code:"validation_error"}}`.

## Error Envelope

```json
{ "error": { "code": "validation_error"|"not_found"|"graph_unavailable"|"model_unavailable"|"mcp_unavailable"|"not_ready"|"internal_error", "message": "redacted, actionable", "details?": [...] }, "request_id"?: string }
```

- `422` validation, `404` not_found, `503` not_ready/graph_unavailable/model_unavailable/mcp_unavailable, `504` timeout, `413` body >1 MB, `500` internal. `RequestValidationError` → `422 validation_error`, `HTTPException`/`StarletteHTTPException` → mapped codes. Bodies never echo `MODEL_API_KEY`, `NEO4J_PASSWORD`, `bolt://user:pass@`, or `"' OR 1=1"` verbatim — redacted via `schemas/errors._sanitize_str`.

## Health vs Readiness

- `GET /health` → `200 {status:"ok", version, uptime_seconds, started_at}` always when process is up, does **not** call `driver.verify_connectivity`. Use for liveness.
- `GET /ready` → probes `await to_thread(driver.verify_connectivity)` (cached 5s), `config_ok`, `agent_ok`. `200 {status:"ready", checks:{neo4j:"ok",...}}` only when all hard checks pass; otherwise `503 {status:"not_ready", checks:{neo4j:"unavailable", reason}}`. During Neo4j outage health stays `200` while ready is `503`. Compose healthcheck must use `/ready`.

## Job Polling Pattern

`POST /repositories` validates `RepositoryRegisterRequest`, derives `repository_id` stably from URL, `await job_store.create(status=queued, stage=CLONING)`, then `asyncio.create_task(run_ingestion(...))` (non-blocking, returns `<500ms` even with mocked 5s ingest). `run_ingestion` → `update(status=running, stage=CLONING)` → `await to_thread(ingestion_service.ingest, stage callbacks)` → `CLONING→EXTRACTING→FILTERING→WRITING_GRAPH→COMPLETED` or `FAILED` with `error` (without advancing `last_successful_ingestion` on failure). Poll `GET /repositories/{id}` or `GET /jobs/{job_id}` for `status`/`stage`/`progress`/`error`/`finished_at`. `GET /repositories` paginated `?limit=20&offset=0` (limit 1..100, default 20) deduplicated by `repository_id`.

## Non-Blocking Guarantee

Every handler is `async` and contains only validation + delegation. Heavy work (`git clone`, `pydriller`, `MERGE` batches, AST parsing) runs via `await asyncio.to_thread(...)` inside the background task. Verified: `POST /repositories` with mocked 5s ingest returns in `<500ms`; `GET /health` and `POST /reports` remain `<200ms` while a job is `running`.

## Thin Routes

`src/chronotrace/api/routes/` contains only HTTP validation, dependency wiring, service delegation, and response shaping. No `MATCH`/`MERGE`/`session.run`, no scoring, no `git clone`/`subprocess`, no prompt construction — that lives in `core/graph`, `core/analytics`, `core/ingestion`, `agent/`, `mcp`. `load_config()` is called only in `lifespan`, never in routes (verified via scan).

## cURL Examples

```bash
# health (liveness, no deps)
curl -f http://localhost:8000/health
# {"status":"ok","version":"0.1.0","uptime_seconds":12.3,"started_at":"..."}  200

# readiness (dependency checks)
curl -f http://localhost:8000/ready
# 200 {"status":"ready","checks":{"neo4j":"ok","config":"ok","agent":"ok"}}
# 503 {"status":"not_ready","checks":{"neo4j":"unavailable","reason":"..."}} when Neo4j down
# health stays 200 while ready is 503

# register repository (non-blocking, job poll)
curl -i -X POST http://localhost:8000/repositories \
  -H "Content-Type: application/json" \
  -d '{"url":"https://github.com/org/repo.git","branch":"main"}'
# 201 {"repository_id":"...","job_id":"...","status":"queued"}  Location + X-Job-Id
curl http://localhost:8000/repositories
# {"repositories":[{"repository_id":"...","url":"...","ingestion_status":"running",...}],"total":1}
curl http://localhost:8000/repositories/<repository_id>
# {"repository_id":"...","job":{"status":"running","stage":"CLONING",...}} → poll until completed
curl http://localhost:8000/jobs/<job_id>
# same envelope by job_id

# deterministic report (no LLM, works without MODEL_API_KEY)
curl -X POST http://localhost:8000/reports \
  -H "Content-Type: application/json" \
  -d '{"repository_id":"<id>","file_path":"src/foo.py"}'
# 200 {"status":"ok","repository_id":"...","target":"src/foo.py","result":{"score":...,"breakdown":...},"evidence":{},"limitations":{}}
# traversal rejected before graph call
curl -X POST http://localhost:8000/reports -d '{"repository_id":"...","file_path":"../secret.py"}'
# 422 {"error":{"code":"validation_error","message":"... traversal ..."}}

# chat (repository-scoped agent, requires MODEL_API_KEY for live model)
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"repository_id":"<id>","question":"What is the regression risk for src/foo.py?"}'
# 200 {"answer":"...","citations":["src/foo.py@..."],"evidence":{},"limitations":"...","conversation_id":"..."}
# 422 missing question, 404 unknown repo, 503 model_unavailable/mcp_unavailable, 504 timeout
```

Run via `uvicorn chronotrace.api.main:app --host 127.0.0.1 --port 8000` after `docker compose up neo4j` and `MODEL_API_KEY` set; `GET /openapi.json` lists all paths, `GET /docs` loads.

## Security & Bounds

- Git URL `1..2048`, reject `\x00`, shell metachars `$()`, `` `` ``, `;`, `|`, `$(`, validated via `AfterValidator(validate_git_url)`.
- `file_path` POSIX repo-relative, `1..4096`, reject absolute, `..` segments (decoded `%2e%2f%5c`, backslash, NUL).
- `question` `1..10000`, JSON body `≤1 MB` → `413` or `422` before processing.
- Oversized `file_path` 4096-char payload rejected or truncated safely; `limit` for file history bounded.
- Lifespan shutdown: `await job_store.cancel_all(reason="shutdown")` 5s, `await mcp_client.aclose()` 5s, `driver.close()` via `to_thread`, then drain; `GET /health` after close → `503` or connection error.

## OpenAPI

`GET /openapi.json` lists `GET /health`, `GET /ready`, `POST /repositories`, `GET /repositories`, `GET /repositories/{repository_id}`, `GET /jobs/{job_id}`, `POST /reports`, `GET /reports`, `POST /chat`. `GET /docs` and `GET /redoc` serve Swagger/ReDoc.
