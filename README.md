# ChronoTrace — Evidence-first codebase risk analysis

ChronoTrace builds a repository-scoped graph of commits, files, and co-change evidence without executing repository code, powering change criticality and regression-risk analytics.

## Quickstart

```bash
pip install -e .
pytest -q
```

## Configuration

Validated `chronotrace.yml` (Pydantic v2, schema v1) — env secrets outside YAML. See [`chronotrace.yml`](./chronotrace.yml) for defaults.

## Ingestion

Ingestion turns a remote Git URL into graph evidence: acquisition → PyDriller extraction → filtering → labeling → batched Neo4j writes. Full details in [`src/chronotrace/ingestion/README.md`](./src/chronotrace/ingestion/README.md).

**Usage**

```python
from chronotrace.ingestion import IngestionService
from chronotrace.config import load_config

config = load_config("chronotrace.yml")
svc = IngestionService(config=config, gateway=graph_gateway)
result = svc.ingest("https://github.com/org/repo.git", branch="main")
print(result.state.status, result.state.counters)
```

**Config options**

| Key | Default | Purpose |
|---|---|---|
| `project.repositories_dir` | `~/.chronotrace/repositories` | Managed clone location (`<repositories_dir>/<slug>-<hash>`) |
| `analysis.filtering.exclude` | `["**/.git/**", "**/__pycache__/**", ...]` | Glob exclusion (gitwildmatch via `pathspec`) |
| `analysis.bug_fix_labels.patterns` | `["\\bfix\\b", "\\bhotfix\\b", ...]` | Case-insensitive fix regex list |
| `analysis.bug_fix_labels.case_sensitive` | `false` | Toggle case sensitivity |
| `analysis.history.max_commits` | `null` | Optional traversal cap |
| `graph.batch_size` | `500` | `UNWIND $batch AS row MERGE` batch size |

**Lifecycle states**

`PENDING → CLONING → EXTRACTING → FILTERING → WRITING_GRAPH → COMPLETED` (or `FAILED` from any state). Each stage records an ISO timestamp; counters (`commits`, `files`, `filteredOut`, `batches`) and redacted error summaries are persisted on `Repository`/`IngestionState`. Query via `svc.get_status(repository_id)`.

- Incremental: `lastIngestedHead` + `git rev-list lastHead..HEAD`, chronological, head advances only on success.
- Rewritten history: `git merge-base --is-ancestor` check → `needsFullReingestion=true` (`RewrittenHistoryError`), requires full re-ingestion.
- Retryable: `svc.retry(repository_id)` resets `FAILED` → `PENDING`.
- Observability: `INFO` logs `Writing batch 1/3 (commits)` without secrets; `filteredOut` reflects `**/__pycache__/**` etc.

**Limitations** — dynamic excludes need re-ingestion; rename threshold conservative (uncertain `DELETE+ADD` stays distinct, `COPY` distinct nodes); force-push not auto-reconciled; no concurrent ingestion lock; shallow history may be truncated.

## Agent (Evidence-First LLM Layer)

The agent is the sole LLM layer, isolated over the deterministic analytics/MCP path. See [`src/chronotrace/agent/README.md`](./src/chronotrace/agent/README.md) for full details.

**Verified Meta Model API (Muse Spark) values** — pinned in `src/chronotrace/agent/model_client.py` header and `AgentConfig` validators; fail closed on mismatch. Checked 2026-11-04 against https://dev.meta.ai/docs.

| Property | Verified value | Source |
|---|---|---|
| Base URL | `https://api.meta.ai/v1` | docs/quickstart “Base URL: https://api.meta.ai/v1” |
| Chat endpoint | `POST https://api.meta.ai/v1/chat/completions` (relative `/chat/completions`) | docs/protocols/chat-completions |
| Auth header | `Authorization: Bearer <MODEL_API_KEY>` (env only, never YAML/log) | docs/authentication |
| Model identifier | `muse-spark-1.3` (pattern `^muse-spark-1\.3(-contributor)?$`) | docs/models, `chronotrace.yml` `agent.model` |
| Tool request | `tools: [{type:"function", function:{name,description,parameters}}]` | docs/tool-calling |
| Tool response | `choices[0].message.tool_calls: [{id, type:"function", function:{name,arguments}}]` | docs/tool-calling |
| Tool result message | `{role:"tool", tool_call_id:"<id>", content:"<JSON string>"}` | docs/tool-calling |
| `reasoning_effort` | top-level `reasoning_effort`; provider values `none\|minimal\|low\|medium\|high\|xhigh\|max` — ChronoTrace validates `low\|medium\|high` (default `medium`) | docs/reasoning |

Tool definitions: five MCP tools from `chronotrace.mcp_server.tools.schemas.CANONICAL_SCHEMAS`, translated via `to_provider_tools` to provider format. No new runtime dependency beyond `httpx`/`mcp` already declared.

Deterministic report path remains available without the model: `AnalyticsService.get_change_criticality(...)` and MCP `get_change_criticality` return structured `status`/`evidence`/`limitations` without `Agent`/`ModelClient`.

## Docker — Build & Compose

Single image `chronotrace:dev` serves both `agent-api` (8000) and `mcp-server` (8001); `neo4j:5-community` provides the graph. No secret is baked into the image or `docker-compose.yml`.

### Hostname split — driver (Bolt) vs Browser (HTTP)

- **Driver (code):** **Inside Compose** `agent-api`/`mcp-server` → `bolt://neo4j:7687` (Compose DNS). `docker-compose.yml` sets `NEO4J_URI=bolt://neo4j:7687`. **On the host** `bolt://localhost:7687` (published `7687:7687`). `chronotrace.yml` keeps `bolt://localhost:7687`; Compose overrides at runtime. Do not use `http://` for driver — `neo4j.GraphDatabase.driver` will fail.
- **Browser (humans):** `http://localhost:7474` on host (published `7474:7474`), `http://neo4j:7474` inside Compose. Open `http://localhost:7474` in your browser to see the graph. Misconfigure driver to `bolt://localhost:7687` inside a container → `GET /ready` stays `503 not_ready` with `neo4j unavailable`.

### Development (from source)

```bash
cp .env.example .env   # fill NEO4J_PASSWORD, MODEL_API_KEY
docker build -t chronotrace:dev .
docker compose --env-file .env up -d --build
docker compose ps            # neo4j healthy before agent-api/mcp-server
docker compose logs -f agent-api
curl -f http://localhost:8000/health   # 200 {status:"ok"} even when neo4j down
curl -f http://localhost:8000/ready    # 200 ready only when neo4j healthy
curl -f http://localhost:8001/health   # mcp health (stub) or /mcp endpoint
# open browser to see graph:
open http://localhost:7474  # Neo4j Browser — login with NEO4J_USER/PASSWORD from .env
```

Published image (no build):
```bash
docker pull ghcr.io/<org>/chronotrace:0.1.0
docker compose --env-file .env up -d
```

### Compose file — `docker-compose.yml`

`neo4j` `image: neo4j:5-community`, `NEO4J_AUTH: ${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD:-password}`, healthcheck `cypher-shell RETURN 1` / `wget http://localhost:7474`, `restart: unless-stopped`, volumes `neo4j_data:/data` + `neo4j_logs:/logs` (survives `down` without `-v`). `agent-api` and `mcp-server` share `build: .` → `image: chronotrace:dev`, `env_file: .env`, `depends_on: neo4j: condition: service_healthy` (mcp before api), healthchecks `curl -f http://localhost:8000/health` / `8001/health`, `restart: unless-stopped`, `chronotrace_data:/app/data` + `./chronotrace.yml:/app/chronotrace.yml:ro` on bridge `chronotrace-net`.

### Volumes & shutdown

- `docker volume ls` shows `neo4j_data`, `neo4j_logs`, `chronotrace_data` after `docker compose down` (no `-v`).
- `docker compose down` within stop grace (10s) drains jobs, `driver.close()` + `mcp.aclose()`, exits 0, no graph corruption. Logs via `docker compose logs agent-api` / `docker logs chronotrace-api`.

### Secrets via env (never baked)

`.env.example` lists placeholders (`NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, `MODEL_API_KEY`, `MCP_PORT`, `API_PORT`, `MODEL_BASE_URL`) with hostname comments; `docker-compose.yml` references only `${VAR}`. `docker history chronotrace:dev` and `docker inspect` show no secret, no `.env`.

### Installed CLI asset location

When installed via `pip install .`, `chronotrace` locates bundled `docker-compose.yml`/`Dockerfile` via `src/chronotrace/cli/compose.py:get_compose_path()` using `importlib.resources` → `src/chronotrace/` → `cwd` fallback, without assuming `cwd` is the repo root.

### Makefile

```bash
make docker-build   # docker build -t chronotrace:dev .
make docker-up      # docker compose --env-file .env up -d --build
make docker-down    # docker compose down
make docker-ps      # docker compose ps
make docker-logs    # docker compose logs -f
```

## Other docs

- Ingestion deep-dive: [`src/chronotrace/ingestion/README.md`](./src/chronotrace/ingestion/README.md) (transport formats, canonicalization vectors, filtering, change handling, labeling)
- Graph schema: `src/chronotrace/graph/`
- Agent layer: [`src/chronotrace/agent/README.md`](./src/chronotrace/agent/README.md) (provider contract, prompts, loop, error handling)
- API: [`src/chronotrace/api/README.md`](./src/chronotrace/api/README.md)

## Development

```bash
openspec validate --all --strict
pytest
```
