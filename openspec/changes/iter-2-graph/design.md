# Design: iter-2-graph — Neo4j Connection, Graph Schema, and Persistence Foundation

## Context

Iteration 1 delivers validated configuration (Pydantic v2 `chronotrace.yml` schema, env secrets, path validation) as the single source of graph settings (URI, database, credentials, batch size, decay). Iteration 2 must provide the first stateful layer: a shared Neo4j driver, a versioned graph schema, and idempotent, repository-isolated persistence that later iterations will write through. No ingestion, static analysis, analytics, or MCP code exists yet; the graph layer is a greenfield internal API.

Constraints: Python 3.12+, `neo4j` driver already declared in `pyproject.toml`, Neo4j 5 Docker image in `docker-compose.yml`, `src` layout under `src/chronotrace/graph/`, no arbitrary Cypher exposure (PLAN §2.2), ingestion retryable/idempotent, and configuration precedence rules from Iter 1.

See `proposal.md` Why/What Changes for motivation; see `specs/graph/spec.md` for normative behavior.

## Goals / Non-Goals

**Goals:**

- Deliver a single application-wide Neo4j driver with deterministic lifecycle, connectivity verification, and clean shutdown.
- Ship a versioned, migration-tracked schema (constraints + indexes + node/relationship definitions) that is idempotent and reviewable as Cypher files.
- Provide narrow, testable repository/gateway interfaces that enforce parameterized Cypher, MERGE idempotency, batching, and repository isolation.
- Establish error mapping to domain exceptions so upper layers never import `neo4j` exceptions.

**Non-Goals:**

- Ingestion, PyDriller extraction, path filtering, fix labeling (Iter 3).
- Static import resolution and IMPORTS edge population from AST (Iter 4).
- Analytics, decay, co-change computation, or evidence contracts (Iter 5).
- MCP tools, agent/model, FastAPI routes, CLI Docker lifecycle beyond connectivity check (later iterations).
- Multi-database / multi-cluster Neo4j topologies, causal clustering config, or custom Neo4j plugins — single database per deployment.
- Automatic schema diffing from live database; migrations are explicit, authored files.

## Decisions

### Decision 1: Single shared driver via factory + application gateway

**Choice:** A `GraphDriver` / `Neo4jConnection` factory that creates one `neo4j.Driver` per process (singleton or dependency-injected instance), reuses it for all sessions, and exposes `verify_connectivity()`, `session()`, and `close()`. FastAPI and CLI will own its lifetime via lifespan / context manager. Repositories receive a `Driver` or `GraphGateway` rather than constructing drivers.

**Rationale:** Driver creation is expensive (connection pool, routing table); per-query drivers exhaust sockets and break transaction semantics. PLAN explicitly forbids per-query drivers.

**Alternatives considered:**
- Per-query driver: rejected — resource exhaustion, violates PLAN § Iteration 2 Scope.
- One session held open: rejected — sessions are not thread-safe and should be short-lived.

### Decision 2: Database-scoped driver with explicit database name in sessions

**Choice:** Store `uri`, `database` (default `neo4j`), `username/password` from validated config; pass `database=` to `session(database=...)` per Neo4j 5 API. Credentials from env only, never YAML.

**Rationale:** Allows testing against a separate database name without changing URI; aligns with Neo4j 5 multi-database support and docker-compose defaults.

**Alternatives considered:**
- URI-only with database in URI: less explicit, harder to test with disposable DBs.

### Decision 3: Cypher migrations as numbered files with tracking node

**Choice:** Directory `src/chronotrace/graph/migrations/` with files like `V001__initial_constraints.cypher`, `V002__initial_indexes.cypher`, `V003__nodes_and_relationships.cypher` (or consolidated `V001`). Runner reads ordered files, queries a `SchemaMigration { version, applied_at, checksum }` node/label to skip applied versions, executes each file in its own transaction with `IF NOT EXISTS` guards (Neo4j 5 supports `CREATE CONSTRAINT ... IF NOT EXISTS`, `CREATE INDEX ... IF NOT EXISTS`). Checksum comparison forces manual intervention on changed-but-applied migration.

**Rationale:** Plain Cypher files are reviewable, diffable, and runnable by tooling; tracking node avoids external migration table and stays inside the graph.

**Alternatives considered:**
- `neo4j-migrations` library: adds Java-side dependency; prefer plain Python runner to keep backend Python-only.
- Schema creation purely via `MERGE` side-effects: not robust for constraints/indexes which require explicit DDL.

### Decision 4: Constraints before indexes, IF NOT EXISTS idempotency

**Choice:** Order migrations: uniqueness constraints first, then indexes, then initial data-shape documentation. Use `IF NOT EXISTS` so runner is idempotent. After init, assert via `SHOW CONSTRAINTS` / `SHOW INDEXES` in integration tests.

**Rationale:** Neo4j constraint creation implicitly creates an index; ordering avoids conflicts and duplicate index creation.

### Decision 5: Stable identifiers and MERGE contract

**Choice:** Canonical IDs:
- `Repository.id = repoId` (stable derived from canonical remote URL or persisted UUID; URL canonicalized to lowercase, trimmed `.git`).
- `Author.id = hash(repository_id + email|name)` — scoped to repository.
- `Commit.id = repository_id + ":" + full_hash`; uniqueness on `Commit.id` plus index on `Commit.hash`.
- `File.id = repository_id + ":" + canonical_path` (POSIX normalized).
All writes use `MERGE (n:Label {id: $id, repository_id: $repoId}) ON CREATE SET ... ON MATCH SET ...` so re-running ingestion is idempotent. Historical path stored as property array `past_paths` when renames occur.

**Rationale:** Satisfies PLAN identity rules (§ Iteration 2 — Identity and relationship rules) and prevents cross-repo collisions.

**Alternatives considered:**
- auto-generated UUID per node: loses stable idempotency on re-ingestion.

### Decision 6: Repository isolation at API boundary

**Choice:** Every repository method requires `repository_id` as first argument or as part of the entity; Cypher always includes `WHERE n.repository_id = $repoId` or matches on `id` that embeds `repoId`. `CO_CHANGED_WITH` and `IMPORTS` writes validate both endpoints share the same `repository_id` before MERGE and fail fast with `GraphWriteError` otherwise.

**Rationale:** Prevents accidental cross-repo contamination; enforces PLAN's repository isolation guarantee.

### Decision 7: Parameterized Cypher only, centralized query templates

**Choice:** Queries live as constants or files in `src/chronotrace/graph/repositories/` or `src/chronotrace/graph/queries/` with `$param` placeholders; callers pass `parameters` dict to `session.run()`. No method accepts a raw query string from outside the graph layer. Lint rule or code review check forbids f-string Cypher.

**Rationale:** PLAN non-negotiable principle: all Cypher must be parameterized; prevents injection and aligns with determinism.

### Decision 8: Batch writes via UNWIND + configurable batch_size

**Choice:** Writer accepts `Iterable[Dict]` and executes `UNWIND $batch AS row MERGE ...` per batch. `batch_size` from validated config (Iter 1) with positive-int validation. Each batch runs in a `session.execute_write` transaction. Caller iterates batches; runner reports batches written for ingestion progress (Iter 3).

**Rationale:** Bounded transactions avoid OOM on large histories; UNWIND is the idiomatic Neo4j bulk pattern.

**Alternatives considered:**
- One giant transaction: risks timeout and memory pressure.

### Decision 9: Domain exception hierarchy

**Choice:** `src/chronotrace/graph/errors.py` with `GraphError` base, subclasses `GraphConnectionError`, `GraphWriteError`, `GraphNotAvailable`, `GraphMigrationError`. Driver maps `neo4j.exceptions.*` to these without leaking `neo4j` types. Messages never include credentials or full Cypher with secrets.

**Rationale:** Upper layers (ingestion/analytics) should depend on stable domain errors, not driver library version.

### Decision 10: Gateway facade + per-entity repositories

**Choice:** `GraphGateway` (or `GraphService`) composes `RepositoryRepository`, `AuthorRepository`, `CommitRepository`, `FileRepository`, `RelationshipWriter`. Each repository owns its label's Cypher and MERGE logic. Gateway is the public entry point for Iter 3+; repositories are independently unit-testable via fakes and disposable Neo4j in integration tests.

**Rationale:** Keeps Cypher confined, avoids circular dependencies, supports PLAN layering (Analytics -> Graph -> Neo4j).

## Risks / Trade-offs

- **Neo4j unavailable at startup** → Gate readiness checks via `verify_connectivity()`; API/ingestion refuse to mark ready and return `GraphNotAvailable` with retry hint. Do not block config validation — connectivity check is separate from parsing.
- **Concurrent schema init from multiple processes** → Use `IF NOT EXISTS` DDL and migration locking: first runner creates a `MigrationLock` node with TTL or uses a `MERGE` + `SET applied_at = coalesce(applied_at, $now)` guard. Document that only one migrator should run in production startup.
- **Migration checksum mismatch (file edited after apply)** → Runner fails with `GraphMigrationError` requiring manual resolution; never silently re-apply changed migration.
- **Batch transaction timeout on very large batch_size** → Validate batch_size positive and bounded (e.g., 1–10_000); ingestion chooses size from config; tests cover boundary.
- **File rename confidence heuristic couples graph layer to Git semantics** → Graph layer stores whatever ingestion provides (confident vs uncertain); it does not implement Git rename detection itself — Iter 3 owns that decision; graph just enforces the "do not silently merge" rule via distinct IDs.
- **Co-change confidence semantics misinterpreted** → Relationship stores directional `confidence` with documentation; analytics layer (Iter 5) must expose limitations text; graph layer does not interpret causality.
- **Driver thread-safety with FastAPI background tasks** → Sessions are short-lived per operation; driver is thread-safe but sessions are not shared across threads — repositories create session per call.
- **Testing without Docker slows CI** → Unit tests use fakes/mocks for driver; integration tests run against Testcontainers/ disposable Neo4j and are marked `integration` to allow selective execution.

## Migration Plan

1. Land `src/chronotrace/graph/` scaffold and `config` graph settings consumption without changing existing CLI/API behavior (no callers yet).
2. Add migration runner and run `V001` on local Neo4j (`docker compose up neo4j`); verify `SHOW CONSTRAINTS` / `SHOW INDEXES`.
3. Gate startup validation: `verify_connectivity()` called from warming/health check; services not marked ready when Neo4j is down (reuses Iter 1 precedence — config validation first, then connectivity).
4. No data migration: empty graph; no rollback beyond dropping constraints/indexes via Cypher file `ROLLBACK` docs.
5. Rollback: if schema init fails, runner leaves version tracking unchanged; re-running succeeds. If production schema already applied, rollback is manual `DROP CONSTRAINT ... IF EXISTS` via next migration, not automatic revert.

## Open Questions

- **Locking for multi-instance migrations:** Should the migration runner acquire a distributed lock in Neo4j or rely on deployment sequencing (only one migrator pod)? Deferred to deployment (Iter 9) — document assumption that a single initializer runs at startup and add lock if concurrent startup is observed.
- **Batch parallelization:** Should batch writes be parallelized per repository? Deferred — keep sequential batches in this iteration; parallelize only if ingestion profiling shows bottleneck.
