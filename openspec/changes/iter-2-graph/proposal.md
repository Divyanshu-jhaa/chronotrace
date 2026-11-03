# Proposal: iter-2-graph — Neo4j Connection, Graph Schema, and Persistence Foundation

## Why

ChronoTrace depends on a durable, queryable historical graph, but no shared Neo4j connectivity, schema, or persistence contracts exist yet. Establishing driver lifecycle, versioned schema, and idempotent persistence now unblocks all downstream ingestion, static-analysis, analytics, and MCP work while enforcing repository isolation and safe retry semantics from the start.

## What Changes

- Implement a shared Neo4j driver lifecycle with application-level driver reuse (no per-query driver creation), configurable URI/database/credentials from validated configuration and environment, explicit `verify_connectivity()` check, and correct session/transaction cleanup on success, failure, and shutdown.
- Define repository-scoped stable identifiers for `Repository`, `Commit`, `File`, and `Author` and enforce them as the identity contract for all writes and reads.
- Create the initial graph schema for nodes (`Repository`, `Author`, `Commit`, `File`) and relationships (`CONTAINS`, `BELONGS_TO`, `AUTHORED`, `MODIFIED`, `REVERTS`, `IMPORTS`, `CO_CHANGED_WITH`) with version-controlled Cypher migration files and migration/version tracking.
- Add uniqueness constraints for stable node identifiers and indexes for frequently queried fields (e.g., `Commit.hash`, `File.path`, `File.is_test`, `Commit.date`, `Repository.name`).
- Implement schema initialization and migration runner that is safe to run repeatedly (idempotent DDL) and records applied migration versions.
- Implement graph repositories / focused persistence modules per entity (`RepositoryRepository`, `AuthorRepository`, `CommitRepository`, `FileRepository`, plus relationship writers) behind a narrow `GraphGateway` or equivalent facade.
- Enforce parameterized Cypher for all reads and writes; forbid string-interpolated Cypher and arbitrary Cypher execution.
- Use `MERGE` on stable identifiers for idempotent writes; repeated writes MUST NOT create duplicate nodes or relationships.
- Add batched writes with configurable `batch_size` sourced from validated configuration.
- Ensure repository isolation: every node and relationship carries and is queried by `repository_id`; cross-repository reads/writes are impossible through the public API.
- Surface database failures through domain-level errors (e.g., `GraphConnectionError`, `GraphWriteError`, `GraphNotAvailable`) without leaking driver internals or secrets.

## Capabilities

### New Capabilities

- `graph`: Neo4j driver lifecycle, schema/migrations (constraints, indexes), node/relationship persistence contracts, parameterized idempotent Cypher, batch writes, session/transaction management, and repository isolation.

### Modified Capabilities

- None — this is the first delivery of the graph layer. No existing `openspec/specs` capability is modified.

## Impact

- **Code:** New modules under `src/chronotrace/graph/` (`driver.py` / `connection.py`, `schema/`, `migrations/`, `repositories/`, `errors.py`, `gateway.py`); configuration consumers read graph settings from the Iteration 1 validated config object rather than raw YAML/env.
- **Dependencies:** `neo4j` Python driver (already in `pyproject.toml`), Neo4j 5 Docker image via `docker-compose.yml` (no new external services).
- **APIs / Systems:** No public HTTP/MCP/API surface in this iteration; internal API is the graph gateway/repository interfaces consumed later by ingestion (`iter-3`), static analysis (`iter-4`), and analytics (`iter-5`).
- **Breaking changes:** None for external consumers. Internal graph contracts are new, not breaking.
- **Operations:** Neo4j must be running for graph-dependent paths; startup validation (Iter 1 config) now gates graph readiness via connectivity check — services MUST NOT report ready when Neo4j is unreachable.
