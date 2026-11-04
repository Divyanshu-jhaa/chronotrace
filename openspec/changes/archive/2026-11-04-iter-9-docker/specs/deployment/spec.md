# Spec Delta — deployment

## Purpose

Provides the Docker image and Compose deployment that lets a clean host run ChronoTrace backend and Neo4j without host Python or database installs, with env-based secrets, persistent volumes, health-gated startup, and documented build/usage.

## ADDED Requirements

### Requirement: Backend Dockerfile builds a single reusable image
The system SHALL provide a `Dockerfile` at the repository root that builds a single backend image containing the ChronoTrace Python application and its runtime dependencies from `pyproject.toml`.

#### Scenario: Docker build succeeds from repository root
- **WHEN** a user runs `docker build -t chronotrace:dev .` from the repository root on a machine with Docker
- **THEN** the build completes without baking secrets or `.env` and produces an image that can run both `agent-api` and `mcp-server` commands

#### Scenario: Image does not contain baked secrets
- **WHEN** an image built from the Dockerfile is inspected via `docker history` and filesystem check
- **THEN** no secret values, `.env` file, or `ARG` secret remain in layers

#### Scenario: Image runs with non-root user
- **WHEN** the built image is run with `docker run --rm chronotrace:dev python -c "import chronotrace; print('ok')"`
- **THEN** the container process does not run as root and the import succeeds

### Requirement: Single image serves both agent-api and mcp-server roles
The system SHALL use the same backend image for the `agent-api` and `mcp-server` Compose services with different startup commands.

#### Scenario: Agent API starts from shared image
- **WHEN** the `agent-api` service starts using the backend image with its API command (`uvicorn chronotrace.api.main:app --host 0.0.0.0 --port 8000`)
- **THEN** the FastAPI application becomes available on the service's port and exposes `GET /health`

#### Scenario: MCP server starts from the same image
- **WHEN** the `mcp-server` service starts using the same backend image with its MCP command (`python -m chronotrace.mcp_server.server --host 0.0.0.0 --port 8001`)
- **THEN** the MCP Streamable HTTP endpoint is reachable and lists the five tools

#### Scenario: Images are identical by digest
- **WHEN** the Compose file references the backend image for both `agent-api` and `mcp-server`
- **THEN** both services resolve to the same image digest or build context

### Requirement: Neo4j service uses official image with environment-driven auth
The system SHALL define a `neo4j` Compose service using the official `neo4j:5-community` (or pinned `neo4j:5`) image with auth sourced from environment variables and no hardcoded password in the Compose file.

#### Scenario: Neo4j starts with env-provided credentials
- **WHEN** `NEO4J_USER` and `NEO4J_PASSWORD` are set in the env file and `docker compose up -d neo4j` is run
- **THEN** Neo4j accepts Bolt connections with those credentials and rejects the hardcoded `neo4j/password` default

#### Scenario: Compose file contains no hardcoded password
- **WHEN** the committed `docker-compose.yml` is inspected
- **THEN** it references `${NEO4J_PASSWORD}` or `${NEO4J_AUTH}` and contains no literal password string

### Requirement: Compose defines services, networking, and published ports
The system SHALL provide a `docker-compose.yml` that defines `neo4j`, `mcp-server`, and `agent-api` services on a shared network with documented port mappings for host access.

#### Scenario: Stack starts with three services
- **WHEN** `docker compose up -d` is run with a valid `.env`
- **THEN** `docker compose ps` shows `neo4j`, `mcp-server`, and `agent-api` running on the shared network

#### Scenario: Host can reach API via published port
- **WHEN** the stack is running
- **THEN** `http://localhost:8000/health` from the host returns a successful response

#### Scenario: Inter-service DNS resolves
- **WHEN** `agent-api` or `mcp-server` resolves the hostname `neo4j` inside the Compose network
- **THEN** it reaches the Neo4j Bolt port without using `localhost`

### Requirement: Health checks, restart policy, and dependency ordering
The system SHALL define Docker health checks for each service, a restart policy, and `depends_on` with `service_healthy` so `agent-api` and `mcp-server` wait for Neo4j readiness.

#### Scenario: Health checks report healthy
- **WHEN** the stack has been up long enough for startup probes to pass
- **THEN** `docker inspect --format='{{.State.Health.Status}}'` for each service reports `healthy`

#### Scenario: Startup ordering respects Neo4j health
- **WHEN** `docker compose up` is run from a clean state
- **THEN** `mcp-server` and `agent-api` do not become healthy before `neo4j` is healthy

#### Scenario: Restart policy recovers from crash
- **WHEN** a service container exits unexpectedly
- **THEN** the configured restart policy brings it back without manual intervention

### Requirement: Persistent volumes for Neo4j and application data
The system SHALL persist Neo4j data in a named volume and ChronoTrace application data (repositories and reports) in a host-mounted or named volume that survives `docker compose down`.

#### Scenario: Graph data survives recreation
- **WHEN** a repository is ingested, then `docker compose down` and `docker compose up -d` are run
- **THEN** the previously ingested graph data remains queryable

#### Scenario: Down without volumes is non-destructive by default
- **WHEN** `docker compose down` is run without `-v`
- **THEN** named volumes remain and `docker volume ls` still lists `neo4j_data`

#### Scenario: Application data volume is mounted
- **WHEN** the Compose file is inspected
- **THEN** it declares a volume for Neo4j (`neo4j_data:/data` or equivalent) and a volume or bind mount for the app data directory

### Requirement: Secrets supplied via environment, never baked
The system SHALL supply `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `MODEL_API_KEY`, and related secrets via environment variables or an env file referenced by Compose, and SHALL NOT commit secrets or bake them into images.

#### Scenario: Env file supplies secrets at runtime
- **WHEN** `docker compose --env-file .env up -d` is run with secrets in `.env`
- **THEN** services start using those values without any secret appearing in `docker-compose.yml` or the Dockerfile

#### Scenario: Image inspection reveals no secrets
- **WHEN** the backend image is inspected with `docker inspect` and `docker history`
- **THEN** no secret value is visible in `Env`, `Config`, or layer commands

#### Scenario: Missing required secret fails with actionable error
- **WHEN** a required env secret is absent and the dependent service starts
- **THEN** the service fails readiness with an error naming the missing variable and without printing the secret value

### Requirement: Container and host hostname split is documented and enforced
The system SHALL use `bolt://neo4j:7687` for container-to-container Neo4j connections and `bolt://localhost:7687` (or `http://localhost:8000` for the API) for host-to-container communication, with startup validation that surfaces misconfiguration.

#### Scenario: Container config uses service hostname
- **WHEN** `agent-api` and `mcp-server` are running inside Compose
- **THEN** their effective `graph.uri` resolves to `bolt://neo4j:7687` (or equivalent hostname-based URI), not `localhost`

#### Scenario: Host CLI uses localhost published port
- **WHEN** the CLI or a host curl targets the API
- **THEN** `http://localhost:8000` reaches the published `agent-api` port

#### Scenario: Misconfigured hostname fails fast
- **WHEN** a container is configured with `bolt://localhost:7687` for its graph URI inside Compose
- **THEN** connectivity or readiness checks fail with an actionable message indicating the hostname should be `neo4j`

### Requirement: Readiness reflects dependency health, not just container start
The system SHALL ensure API readiness becomes successful only after required dependencies (Neo4j connectivity and required configuration) are ready.

#### Scenario: Readiness is false while Neo4j is unavailable
- **WHEN** `docker compose up` is in progress and Neo4j is not yet healthy
- **THEN** `GET /readiness` on `agent-api` returns not-ready even though the container is running

#### Scenario: Readiness becomes true after dependencies recover
- **WHEN** Neo4j becomes healthy and secrets are valid
- **THEN** `GET /readiness` transitions to success

### Requirement: Logs are accessible and shutdown is reliable
The system SHALL make service logs accessible via standard Compose/Docker log commands and SHALL handle `SIGTERM`/`docker compose down` with a graceful shutdown that does not corrupt graph data.

#### Scenario: Logs are available via Compose
- **WHEN** `docker compose logs agent-api` or `docker logs <container>` is run
- **THEN** application logs for that service are returned

#### Scenario: Graceful shutdown closes connections
- **WHEN** `docker compose down` is issued while services are healthy
- **THEN** containers exit with code 0 within the stop grace period and Neo4j data remains consistent on the next start

### Requirement: Build and usage documentation and installed-CLI asset location
The system SHALL document development image builds and published-image usage, and SHALL ensure the packaged Compose and Docker assets are locatable when the CLI is installed outside the source checkout.

#### Scenario: Documentation describes both dev and published workflows
- **WHEN** the README or `docs/` is consulted
- **THEN** it describes `docker build -t chronotrace:dev .` and `docker compose up --build` for development and referencing a published image or `ghcr.io` tag for non-dev usage

#### Scenario: Installed CLI can locate Compose assets
- **WHEN** the package is installed and the CLI is run from an arbitrary directory
- **THEN** it can resolve the bundled `docker-compose.yml` (or its template/reference path) without assuming the current working directory is the source root
