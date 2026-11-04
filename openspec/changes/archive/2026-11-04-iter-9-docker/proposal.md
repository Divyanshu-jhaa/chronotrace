# Proposal: Iteration 9 — Docker Images and Compose Deployment

## Why

ChronoTrace has a Python backend (API, agent, MCP, ingestion, analytics) and a Neo4j graph, but no containerized deployment. Users currently must install backend Python dependencies and Neo4j on the host to run the stack. Iteration 9 delivers a production-ready local deployment via a single reusable backend image and a Compose stack so a clean machine with only Docker and Compose can start all services, persist data, and keep secrets out of images and committed files.

## What Changes

- **Backend Dockerfile** — Create `Dockerfile` at the repository root that builds a single backend image containing the ChronoTrace Python application and its runtime dependencies. Use a `python:3.12-slim` (or equivalent) base, install system deps for Git operations, copy `pyproject.toml` / `src/` / `README.md`, run `pip install .`, expose `8000`/`8001` as needed, set a non-root user, and define a default entrypoint/cmd that can be overridden per service. Do not bake secrets, `.env`, or local config into image layers. Keep the build cache-friendly (dependency layer before source) and produce a deterministic, minimal image.
- **Single image, two services** — Use the same backend image for both `agent-api` (FastAPI + agent + ingestion endpoints, from Iteration 8) and `mcp-server` (MCP server with Streamable HTTP, from Iteration 6) with different startup commands (e.g., `uvicorn chronotrace.api.main:app --host 0.0.0.0 --port 8000` vs `python -m chronotrace.mcp_server.server --host 0.0.0.0 --port 8001`). No code duplication; the image entry is the application, the Compose command selects the role.
- **Official Neo4j service** — Use the official `neo4j:5-community` (or pinned `neo4j:5`) image for the `neo4j` service. Configure `NEO4J_AUTH` from environment (`${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD}`), expose Bolt `7687` and Browser `7474`, and set APOC/plugins only if already required. No custom Neo4j Dockerfile unless a plugin forces it.
- **Compose services, networking, and ports** — Define `docker-compose.yml` with three services (`neo4j`, `mcp-server`, `agent-api`) on a shared Compose network (`chronotrace` or default). `agent-api` and `mcp-server` resolve Neo4j via the Compose service hostname `neo4j` (not `localhost`); host CLI/API communication uses a published `agent-api` port (e.g., `8000:8000`). `mcp-server` exposes `8001` internally and optionally to the host. All ports documented in `.env.example` and README.
- **Health checks, restart, and dependencies** — Add Docker `healthcheck` for each service: `neo4j` via `cypher-shell` or `wget http://localhost:7474` / Bolt probe; `agent-api` via `GET /health` and `GET /readiness` (Iteration 8); `mcp-server` via MCP health/readiness (Iteration 6) or a lightweight HTTP probe. Use `depends_on` with `condition: service_healthy` for `agent-api`/`mcp-server` → `neo4j` so Compose startup ordering respects readiness, not just container start. Set `restart: unless-stopped` (or `on-failure`) and ensure graceful shutdown (`SIGTERM` → drain → `driver.close()`).
- **Volumes and persistence** — Persist Neo4j data in a named volume `neo4j_data` (and optionally `neo4j_logs`). Persist ChronoTrace application data (cloned repositories under `repositories_dir` and generated reports) in host-mounted or named volumes (e.g., `chronotrace_data` mapped to the configured `data_dir`/`repositories_dir` inside the container). Verify that `docker compose down` preserves volumes and `docker compose down -v` is the explicit destructive path. Document the volume layout and how the CLI's installed package locates the compose file when run outside the source checkout.
- **Secrets via environment, never in images** — Supply `NEO4J_USERNAME`/`NEO4J_PASSWORD`/`MODEL_API_KEY`/`MODEL_BASE_URL` etc. through environment variables or an env file (`--env-file .env` / `env_file:` in Compose). Do not `COPY .env` or `ARG` secrets that persist in layers. Document `.env.example` → `.env` flow and validate that `docker history` / `docker inspect` do not expose secret values beyond the runtime env. Lint CI checks that no secret is committed.
- **Container vs host addressing invariants** — Internal `graph.uri` for `agent-api`/`mcp-server` containers MUST be `bolt://neo4j:7687` (Compose hostname), while the default `chronotrace.yml` for host execution remains `bolt://localhost:7687`. The API client's base URL for the CLI on the host MUST be `http://localhost:8000`. Compose and application config document this split explicitly; startup validation surfaces a clear error if the wrong hostname is configured for the runtime context.
- **Logs, shutdown, and docs** — Ensure `docker compose logs -f` shows each service's application logs (no suppression). Provide `Makefile` targets or documented `docker compose up/down/logs` commands and dev vs published-image workflows (`docker build -t chronotrace:dev .` and `docker compose up --build`). Ensure the packaged Compose/Docker assets are locatable from an installed CLI (e.g., via `importlib.resources` or a known package data path) per PLAN.md requirement.
- **No unrelated scope** — Do not introduce Kubernetes manifests, CI publish pipelines, or multi-language parsing.

## Capabilities

### New Capabilities

- `deployment`: Docker images and Compose deployment — backend Dockerfile (single image for `agent-api` and `mcp-server`), official Neo4j service, Compose services/networking/ports/health-checks/restart/depends_on, named volumes for Neo4j and application data, secrets via env/env-file (not baked), hostname split (`neo4j` inside Compose vs `localhost` on host), startup ordering and readiness gating, logs and reliable shutdown, and build/published-image documentation with installed-CLI asset location.

### Modified Capabilities

- None — no existing shipped `openspec/specs` capability is modified. Iterations 0–8 established foundation, config, graph, ingestion, static analysis, analytics, MCP, and API; this iteration adds a deployment layer consumed by the CLI (`iter-10`) but does not change prior spec-level behavior.

## Impact

- **Code:** New/rewritten `Dockerfile` (backend image), rewritten/extended `docker-compose.yml` (three services, healthchecks, volumes, env_file, networks, depends_on), optional `.dockerignore`, updated `.env.example` and `chronotrace.yml.example` comments for Compose hostnames vs localhost, optional `Makefile` targets, and documentation (`README.md` / `docs/`) for `docker build` and `docker compose up` workflows. No changes to `src/chronotrace/api/`, `src/chronotrace/mcp_server/`, or analytics/ingestion Python logic beyond respecting config-driven `graph.uri` and `mcp_server.url` values.
- **Dependencies:** No new Python runtime dependencies; Docker Engine + Compose plugin become the only host prerequisites. Optional pin of `neo4j:5-community` image tag; backend image base `python:3.12-slim` documented with rationale and alternatives.
- **APIs / Systems:** No new HTTP endpoints. Existing `GET /health` and `GET /readiness` (Iteration 8) and MCP health (Iteration 6) are now consumed by Compose healthchecks. Internal service discovery changes (Compose DNS `neo4j`) but does not alter external API contracts.
- **Operations:** `docker compose up` starts the full stack; `docker compose down` stops without deleting named volumes; `docker compose logs` surfaces service logs. Development workflow uses local build, published workflow uses a pre-built image (e.g., `ghcr.io/.../chronotrace:latest`) referenced in Compose.
- **Breaking changes:** None for external API consumers. Hosts that previously relied on hardcoded `NEO4J_AUTH: neo4j/password` in the current `docker-compose.yml` must move to env-based auth; the hardcoded value is removed.
- **Security:** Secrets remain env-only; image layers scanned for leaked secrets. Neo4j Browser port optionally bound to `127.0.0.1` in dev to reduce exposure.

## Out of Scope (Explicitly Excluded)

- CLI commands `init`/`up`/`down`/`status`/`add`/`chat`/`report` (Iteration 10) — Compose invocation remains manual `docker compose up/down` in this iteration.
- Web UI (Iteration 11), backtesting, SZZ, function/symbol-level tracking, real coverage, embeddings, multi-language parsing (PLAN §7 exclusions, Iteration 12).
- Kubernetes, Helm, ECS, or cloud deployment manifests; CI registry publish automation beyond documented `docker build`/`docker tag`/`docker push` notes.
- Neo4j clustering, custom plugins, or performance tuning beyond the single-instance `neo4j:5-community` service.
- Code execution of repository contents at build time or runtime.

## Acceptance Criteria (Iteration-level)

Maps to PLAN.md Iteration 9 — Scope and Completion gate:

- A clean machine with Docker and Compose can start the stack with the documented command (e.g., `cp .env.example .env && docker compose up --build -d` then `curl http://localhost:8000/health` / readiness) without manually installing backend Python deps or Neo4j on the host.
- Neo4j data persists after `docker compose stop` / `docker compose down` and restart (named volume `neo4j_data` survives; restarting stack shows previously ingested repository still in graph).
- `agent-api` and `mcp-server` containers can reach Neo4j via `bolt://neo4j:7687`; host CLI/API communication uses `http://localhost:8000` via published port. No service is hardcoded to `localhost` for container-to-container traffic.
- API readiness (`GET /readiness`) becomes successful only after Neo4j and required secrets are ready; Compose `depends_on: service_healthy` for `neo4j` prevents premature `agent-api`/`mcp-server` readiness success; container start does not imply readiness.
- Secrets are not present in image layers (`docker history` / `docker inspect` / `COPY .env` check) or committed files; `.env` remains gitignored and `.env.example` contains only placeholders.
- Service logs are accessible via `docker compose logs` / `docker logs` without extra tooling.
- `docker compose down` does not delete persistent graph data; data loss requires explicit `docker volume rm` / `down -v`.
- A full smoke test succeeds from a clean Compose environment (build → up → wait for healthy → `GET /health` ok → `GET /readiness` ok after Neo4j healthy → optional `POST /repositories` or mock ingestion ping → logs visible → `down` → `up` → data still present).

## Dependencies and Sequencing

- **Depends on:** Iteration 1 (validated `chronotrace.yml` schema, env secrets, precedence, `graph.uri`/`database`/`batch_size`), Iteration 2 (driver reuse, `verify_connectivity()`, repository isolation), Iteration 6 (MCP server with Streamable HTTP on `:8001`, health behaviour), Iteration 8 (FastAPI `main.py` lifespan, `GET /health` vs `GET /readiness`, `uvicorn` entry, graceful `driver.close()`), and existing `pyproject.toml` runtime dependencies.
- **Unblocks:** Iteration 10 (CLI `up`/`down`/`status` wrapping `docker compose` and locating bundled compose assets), Iteration 11 (UI consuming `agent-api` at `http://localhost:8000`), Iteration 12 (integration tests against `docker compose` stack, reliability and backtest harness).
