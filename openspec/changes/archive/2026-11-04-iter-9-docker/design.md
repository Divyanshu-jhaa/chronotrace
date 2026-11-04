# Design: Iteration 9 — Docker Images and Compose Deployment

## Context

After Iterations 1–8 ChronoTrace has validated configuration (`chronotrace.yml` via Pydantic v2 with env-secret overlay and `graph.uri`/`database`/`batch_size`), a shared Neo4j driver with `verify_connectivity()` and repository isolation, historical ingestion via PyDriller, current `IMPORTS` via AST, deterministic analytics with `ok`/`insufficient_history`/`not_found` contracts, an MCP server exposing five tools over Streamable HTTP `:8001`, and a FastAPI backend (`agent-api`) on `:8000` with `GET /health` (process) vs `GET /readiness` (dependencies) and graceful `driver.close()` on shutdown. The repository currently ships an empty `Dockerfile` and a minimal `docker-compose.yml` that only runs `neo4j:5-community` with hardcoded `NEO4J_AUTH: neo4j/password` and no health checks, volumes beyond `neo4j_data`, or backend services.

See `proposal.md` for motivation and `specs/deployment/spec.md` for normative behavior; this document covers *how* to complete the deployment layer without changing Python application logic.

Constraints: Docker Engine + Compose plugin are the only new host prerequisites; secrets remain env-only; no Kubernetes or CI publish automation in this iteration; single-instance Neo4j; `src` layout and `pyproject.toml` unchanged; host and container hostnames differ.

## Goals / Non-Goals

**Goals:**
- Ship a single backend image that contains the ChronoTrace Python application and runs as either `agent-api` or `mcp-server` via command override.
- Extend `docker-compose.yml` to a three-service stack (`neo4j` + `mcp-server` + `agent-api`) with correct networking, ports, health checks, `depends_on: service_healthy`, restart policy, and persistent volumes, and make it runnable from a clean machine with only Docker/Compose and a prepared `.env`.
- Ensure env-based secrets, hostname correctness (`neo4j` inside Compose vs `localhost` on host), readiness gating, log visibility, and graceful shutdown without data loss.
- Document dev vs published-image usage and ensure bundled Compose assets are discoverable from an installed CLI.

**Non-Goals:**
- CLI `up`/`down`/`status` wrappers (Iteration 10), web UI (Iteration 11), backtesting/hardening (Iteration 12).
- Kubernetes, Helm, ECS, or cloud manifests; automated registry publishing and image signing.
- Neo4j clustering, custom plugins, or non-trivial performance tuning.
- Changing FastAPI route shapes, MCP tool contracts, analytics formulae, ingestion, or static-analysis logic.
- Encrypted secret storage or vault integration — env file only for MVP.

## Decisions

### Decision 1: Backend Dockerfile — `python:3.12-slim` single image, cache-friendly layers, non-root

**Choice:**
```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .
RUN useradd -m appuser
USER appuser
EXPOSE 8000 8001
CMD ["uvicorn", "chronotrace.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
Plus a `.dockerignore` that excludes `.env`, `.git`, `__pycache__`, `*.egg-info`, `.venv`, `myenv`, `htmlcov`, `.mypy_cache`, `.ruff_cache`, and build artifacts. `COPY pyproject.toml` before `COPY src/` is attempted but with `src` layout `pip install .` requires both, so the layer split is `pyproject.toml + README.md` then `src/` then `pip install`; future optimization can use `pip install --no-build-isolation` or multi-stage if build time matters. Final image default is the API; `mcp-server` overrides `command`.

**Rationale:** `python:3.12-slim` matches `requires-python >=3.12` and keeps the image small while providing `apt-get` for `git` (required by ingestion) and `curl` (health probes). Installing via `pip install .` respects the existing `setuptools` `src` layout without a new build system. Non-root reduces blast radius. Cache-friendly ordering avoids re-installing deps when only source changes (though the current `src` + `pyproject.toml` coupling limits the benefit).

**Alternatives considered:**
- Alpine (`python:3.12-alpine`) — smaller but musl incompatibilities with `neo4j` driver wheels and slower `pip` builds; rejected.
- Multi-stage build (builder + runtime) — cleaner but premature before profiling; single-stage is simpler and easier to debug for `docker history` secret checks.
- `uv`/`poetry` base image — adds a new toolchain not in `pyproject.toml`; rejected.

### Decision 2: Single image, command override — Compose `build:` shared, `command:` per service

**Choice:** `docker-compose.yml` uses `build: {context: ., dockerfile: Dockerfile}` for both `agent-api` and `mcp-server` (or an `x-backend-build` anchor). `agent-api` runs `uvicorn chronotrace.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers`; `mcp-server` runs `python -m chronotrace.mcp_server.server --host 0.0.0.0 --port 8001` (exact module path verified against the landed `mcp_server` entry in Iteration 6). No separate Dockerfiles, no per-service image divergence.

**Rationale:** PLAN §3.3 requires one backend image for both services; a shared build block guarantees identical layers and digest.

**Alternatives considered:**
- Two Dockerfiles (`Dockerfile.api`, `Dockerfile.mcp`) — duplicates maintenance and risks drift; rejected.
- One image with an entrypoint that switches on env (`ROLE=api|mcp`) — plausible but hides intent in `docker-compose ps`; explicit `command:` is clearer.

### Decision 3: Official Neo4j image unchanged, auth via `NEO4J_AUTH` from env

**Choice:**
```yaml
neo4j:
  image: neo4j:5-community
  environment:
    NEO4J_AUTH: ${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD:?set NEO4J_PASSWORD in .env}
    # Optional: NEO4J_dbms_default__database: ${NEO4J_DATABASE:-neo4j}
  ports:
    - "7474:7474"
    - "7687:7687"
  volumes:
    - neo4j_data:/data
    - neo4j_logs:/logs
  healthcheck:
    test: ["CMD-SHELL", "cypher-shell -u ${NEO4J_USER:-neo4j} -p ${NEO4J_PASSWORD} 'RETURN 1' || wget -qO- http://localhost:7474 || exit 1"]
    interval: 10s
    timeout: 5s
    retries: 10
    start_period: 30s
  restart: unless-stopped
```
Removes the hardcoded `NEO4J_AUTH: neo4j/password`. `cypher-shell` is present in the official image; `wget` fallback covers early boot before auth is ready.

**Rationale:** Minimal delta from the existing `docker-compose.yml`, respects env-secret policy, and provides a health signal that actually queries the DB.

**Alternatives considered:**
- `NEO4JLABS_PLUGINS: '["apoc"]'` — not needed until analytics requires it; deferred.
- Custom Neo4j Dockerfile — unnecessary.

### Decision 4: Compose networking and ports — shared default network, hostnames, and bindings

**Choice:**
```yaml
services:
  agent-api:
    build: .
    ports: ["8000:8000"]
    environment:
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_DATABASE: ${NEO4J_DATABASE:-neo4j}
      NEO4J_USERNAME: ${NEO4J_USER:-neo4j}
      NEO4J_PASSWORD: ${NEO4J_PASSWORD}
      MODEL_API_KEY: ${MODEL_API_KEY}
      MCP_SERVER_URL: http://mcp-server:8001/mcp
    env_file: [.env]
    depends_on:
      neo4j: {condition: service_healthy}
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 20s

  mcp-server:
    build: .
    command: ["python", "-m", "chronotrace.mcp_server.server", "--host", "0.0.0.0", "--port", "8001"]
    ports: ["8001:8001"]  # or expose only internally if host does not need it
    environment:
      NEO4J_URI: bolt://neo4j:7687
      ...
    depends_on:
      neo4j: {condition: service_healthy}
```
Default Compose network provides DNS `neo4j`, `mcp-server`, `agent-api`. Host CLI uses `http://localhost:8000`. `chronotrace.yml.example` documents both: `uri: "bolt://localhost:7687"` for host runs and a comment `# inside Compose: bolt://neo4j:7687`.

**Rationale:** Makes the hostname split explicit and testable; Compose DNS is the standard container-to-container path.

**Alternatives considered:**
- `network_mode: host` — breaks portability (Windows/macOS) and isolation; rejected.
- Separate `chronotrace` network — optional explicitness but default network already isolates per project; either is acceptable.

### Decision 5: Health checks and `depends_on: service_healthy` for readiness gating

**Choice:** Per the snippet above. `agent-api` health uses `GET /health` (liveness); readiness `GET /readiness` is not the Docker health target but is the Compose-visible signal that API waits for Neo4j. `neo4j` health uses `cypher-shell`/`wget`. `mcp-server` health uses its MCP/health endpoint or `GET /health` if the MCP server exposes one (adapt to the landed Iteration 6 route). Intervals 10s, retries 10, `start_period` generous to avoid flapping on cold mounts.

**Rationale:** Satisfies "container start does not mean DB readiness" without polling from the CLI.

**Alternatives considered:**
- `curl -f http://localhost:8000/readiness` as the `agent-api` Docker health — would mark the container unhealthy during transient Neo4j blips; keeping health as liveness and readiness as a separate probe avoids restart loops. Either is defensible; liveness is chosen.

### Decision 6: Volumes — named `neo4j_data` plus app data host mount or named volume

**Choice:**
```yaml
volumes:
  neo4j_data:
  neo4j_logs:
  chronotrace_data:

services:
  neo4j:
    volumes: [neo4j_data:/data, neo4j_logs:/logs]
  agent-api:
    volumes: [chronotrace_data:/app/data]
    # Alternative: ["~/.chronotrace:/home/appuser/.chronotrace"] or "${CHRONOTRACE_DATA_DIR:-./data}:/app/data"
  mcp-server:
    volumes: [chronotrace_data:/app/data]  # read-only if it never writes clones
```
`chronotrace_data` maps to the in-container path that `config.models.ProjectConfig.data_dir`/`repositories_dir` resolve to (e.g., `/app/data` or `/home/appuser/.chronotrace`). The Pydantic config's path expansion still works inside the container; the volume guarantees `down` without `-v` preserves clones and reports. Document both the named-volume and bind-mount options; bind-mount is useful for `code` on the host to inspect clones.

**Rationale:** Named volumes survive `down`; bind mounts give host visibility. Offering both covers dev vs CI.

**Alternatives considered:**
- Bind-mount only — fragile when host path differs per OS; named volumes are more portable for CI.
- No app volume, clone into container ephemeral FS — loses data on recreation; rejected.

### Decision 7: Secrets — `env_file: [.env]` plus explicit `environment:` references, no `COPY .env`

**Choice:** `docker-compose.yml` lists `env_file: [.env]` and per-service `environment:` entries that reference `${VAR}`. `.env` stays gitignored; `.env.example` contains placeholders and the docs instruct `cp .env.example .env && edit`. No `ARG NEO4J_PASSWORD` in the Dockerfile. Startup code validates secrets via `validate_startup(config, env)` and never logs values.

**Rationale:** Keeps secrets out of layers and committed files while letting `docker compose config` resolve them at runtime.

**Alternatives considered:**
- Docker secrets (`secrets:`) — heavier, not in Compose spec v2 subset used by current file; deferred.

### Decision 8: Installed-CLI asset location — `importlib.resources` or package-relative path

**Choice:** CLI's Docker helper (`src/chronotrace/cli/docker.py` in Iteration 10, stubbed or documented now) will locate Compose assets via `importlib.resources.files("chronotrace") / "compose" / "docker-compose.yml"` or by resolving relative to the installed package's `__file__`. For Iteration 9 the deliverable is that `Dockerfile` and `docker-compose.yml` remain at the repo root and are included as package data via `[tool.setuptools.package-data]` or `MANIFEST.in` (`include Dockerfile docker-compose.yml .env.example chronotrace.yml.example`) so a `pip install` places them where the helper can find them.

**Rationale:** Meets PLAN's "Ensure the packaged Compose and Docker assets can be located when the CLI is installed."

**Alternatives considered:**
- Requiring the user to be in the source checkout for `docker compose` — contradicts PLAN.

### Decision 9: Logging and shutdown — stdout logging, `SIGTERM` handling, graceful close

**Choice:** Python services log to stdout (no file). Compose `logs` captures them. Application `lifespan` installs signal handlers that on `SIGTERM` stop accepting new requests, drain in-flight, and call `driver.close()` / `client.close()`. Neo4j's default `stop_grace_period: 30s` suffices; document `docker compose down --timeout 30`.

**Rationale:** Keeps logging portable and shutdown safe.

## Risks / Trade-offs

- **Neo4j health probe without `curl`/`cypher-shell` in the backend image** → Mitigation: Neo4j's own image has `cypher-shell`; backend images install `curl`. If `cypher-shell` auth races, `wget http://localhost:7474` is a liveness fallback; tune `retries`/`start_period` rather than weakening the check.
- **`docker compose healthcheck` not supported on older Compose v1** → Mitigation: document Compose v2 requirement (`docker compose version`); provide a `Makefile` fallback that polls `GET /health` instead of relying solely on `depends_on: service_healthy`.
- **Single backend image rebuild invalidates both services** → Mitigation: shared `build:` anchor means one `docker compose build` rebuilds once; acceptable coupling for MVP.
- **Volume permission mismatch (non-root `appuser` writing to mounted dir owned by root/host)** → Mitigation: ensure `chronotrace_data` is owned by `appuser` (`RUN mkdir -p /app/data && chown appuser:appuser /app/data`) and document UID remapping for bind mounts (`user: "${UID:-1000}:${GID:-1000}"` or `fix-perms` entrypoint).
- **Hostname split misconfiguration (user sets `NEO4J_URI=bolt://localhost...` inside Compose)** → Mitigation: startup `validate_startup` logs a warning when running inside a container (detected via `/.dockerenv` or `KUBERNETES_SERVICE_HOST` absent) and `NEO4J_URI` contains `localhost`; healthcheck failure message names `neo4j`.
- **Published ports collide on host (8000/7474/7687 in use)** → Mitigation: document env overrides `API_PORT`, `NEO4J_HTTP_PORT`, etc., and template them in `docker-compose.yml` as `${API_PORT:-8000}:8000`.
- **Large image from `COPY .` including dev artifacts** → Mitigation: strict `.dockerignore` keeps the context small and excludes secrets/build caches.
- **CI without Docker cannot run smoke tests** → Mitigation: mark Compose smoke tests as `integration` / `docker` and gate on `docker info` availability; pure unit tests remain green without Docker.

## Migration Plan

1. Land `Dockerfile`, `.dockerignore`, and extended `docker-compose.yml` without changing `src/chronotrace/` application code; verify `docker compose config` renders with no literal secrets.
2. Update `.env.example` to include the Compose-relevant variables (`NEO4J_USER`, `NEO4J_PASSWORD`, `MODEL_API_KEY`, `MODEL_BASE_URL`, `API_PORT`, `MCP_PORT`, `NEO4J_DATABASE`) with documented defaults; add a header comment pointing to the hostname split.
3. Add `chronotrace.yml.example` comments indicating host vs Compose URIs.
4. Document build and run workflows in `README.md` and `Makefile` (`build`, `up`, `down`, `logs`, `smoke`). No DB migration.
5. Rollback: `git checkout -- Dockerfile docker-compose.yml .env.example` restores prior single-`neo4j` stack; named volumes are untouched.

## Open Questions

- **Should the MCP `8001` port remain published to the host or only exposed on the Compose network?** Defaulting to published for debugging; Iteration 10's CLI may switch to internal-only and proxy through `agent-api` if direct host access proves unnecessary.
- **Should the backend image use a `venv` inside the container or system `pip`?** System `pip` suffices; a `venv` would isolate but adds indirection with no current pin conflict.
- **Should `.dockerignore` exclusions match `.gitignore` exactly or be a curated subset?** Curated: exclude secrets, caches, and large mounts but include `chronotrace.yml.example` and `README.md` for `COPY` correctness.
