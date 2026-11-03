# Tasks: iter-2-graph — Neo4j Connection, Graph Schema, and Persistence Foundation

## 1. Scaffold graph package and wire validated configuration

- [ ] Create `src/chronotrace/graph/__init__.py`, `errors.py`, `driver.py` (or `connection.py`), `gateway.py`, `repositories/` package, and `migrations/` directory per `PLAN.md` §4 (`src/chronotrace/core/graph/` historic alias — implement under `src/chronotrace/graph/` per current repo layout; keep alias import if existing code expects `core/graph`).
- [ ] Consume graph settings from Iter 1 validated config object (`neo4j.uri`, `neo4j.database`, `neo4j.username`/`password` from env, `batch_size`, etc.) — do not re-parse YAML or env inside graph layer.
- [ ] Map env credentials (`NEO4J_USERNAME`, `NEO4J_PASSWORD` / `NEO4J_AUTH`) without logging values.

**Test criteria:** Unit test imports `chronotrace.graph` without requiring live Neo4j; config object supplies graph settings and missing credentials raise config-level error (Iter 1 contract).

## 2. Implement shared Neo4j driver lifecycle with reuse

- [ ] Implement `GraphDriver`/`Neo4jConnection` factory that creates a single `neo4j.Driver` per process, exposes `verify_connectivity()`, `session(database=...)`, and `close()`, and is safe to share across repositories.
- [ ] Ensure `verify_connectivity()` calls `driver.verify_connectivity()` and translates failures to `GraphConnectionError`/`GraphNotAvailable`.
- [ ] Ensure `close()` is idempotent and releases the pool; subsequent use raises a clear domain error.

**Test criteria:**
- Two successive repository calls share the same driver instance (spy/mock asserts one driver creation).
- `verify_connectivity()` succeeds against a disposable Neo4j container and fails with `GraphConnectionError` when Neo4j is stopped or credentials are wrong.
- `close()` followed by a query raises a domain error; no session leak detected (driver closed, session context managers exercised).

## 3. Define domain error hierarchy

- [ ] Add `src/chronotrace/graph/errors.py` with `GraphError`, `GraphConnectionError`, `GraphNotAvailable`, `GraphWriteError`, `GraphMigrationError` (include `GraphValidationError` if repo isolation violated).
- [ ] Ensure no graph module re-raises `neo4j.exceptions.*` directly; all Neo4j exceptions are caught and mapped.
- [ ] Ensure error messages never contain credentials, raw connection strings, or interpolated Cypher with secrets.

**Test criteria:** Unit test triggers a mocked `neo4j.exceptions.ServiceUnavailable` and asserts the public API raises `GraphConnectionError` with a sanitized message.

## 4. Author version-controlled Cypher migrations for constraints and indexes

- [ ] Create `src/chronotrace/graph/migrations/V001__initial_constraints.cypher` — uniqueness constraints:
  - `Repository.id`
  - `Author.id`
  - `Commit.id`
  - `File.id`
  All with `IF NOT EXISTS` and `REQUIRE ... IS UNIQUE`.
- [ ] Create `V002__initial_indexes.cypher` — indexes for `Commit.hash`, `File.path`, `File.is_test`, `Commit.date`, `Repository.name` with `IF NOT EXISTS`.
- [ ] Create `V003__schema_documentation.cypher` if needed (relationship type docs or additional constraints like `Commit.repository_id` existence); OR consolidate into V001/V002 with comments— document choice in runner.
- [ ] Store checksums alongside each migration file.

**Test criteria:** Migration files exist, are valid Cypher, contain `IF NOT EXISTS`, and are listed by the runner in lexicographic order.

## 5. Implement migration runner and version tracking

- [ ] Implement `SchemaManager` / `MigrationRunner` that: discovers `V*.cypher` files, reads `SchemaMigration` tracking nodes, skips applied versions, runs each file in its own transaction, records `version`, `checksum`, `applied_at`, and fails with `GraphMigrationError` on checksum mismatch for an already-applied version.
- [ ] Make runner idempotent: repeated invocations without file changes produce zero new writes and no errors.
- [ ] Provide `initialize_schema()` helper that invokes the runner to latest version.

**Test criteria:**
- Fresh database: runner applies all migrations and `SHOW CONSTRAINTS`/`SHOW INDEXES` confirms 4 constraints + 5 indexes.
- Re-running runner immediately succeeds and reports 0 migrations applied.
- Editing an already-applied migration file causes `GraphMigrationError` until resolved.

## 6. Implement session and transaction discipline

- [ ] Use `with driver.session(database=...) as session:` and `session.execute_write(...)` / `session.execute_read(...)` patterns everywhere; no long-lived sessions.
- [ ] Ensure every path closes session via context manager or finally block even when Cypher or mapping raises.
- [ ] Verify no method holds a session as instance state.

**Test criteria:** Integration test that forces a transaction failure still closes the session (no leaked sessions; subsequent query succeeds).

## 7. Define node and relationship model with repository-scoped IDs

- [ ] Model `Repository`, `Author`, `Commit`, `File` dataclasses/Pydantic models with fields from PLAN § Iteration 2 Initial node types.
- [ ] Implement canonical ID derivation: `Repository.id` from canonical URL, `Commit.id = repoId:hash`, `File.id = repoId:posixPath`, `Author.id = repoId:normalizedEmail|name`.
- [ ] Normalize file paths to POSIX and preserve `past_paths` for renames; preserve unknown additions/deletions as `None`/`null` not `0`.

**Test criteria:** Unit tests for ID derivation cover same-hash cross-repo distinctness, POSIX normalization on Windows-style input, and `None` preservation for unknown stats.

## 8. Implement per-entity repositories with parameterized Cypher

- [ ] Implement `RepositoryRepository`, `AuthorRepository`, `CommitRepository`, `FileRepository` each owning its label's Cypher templates with `$param` placeholders.
- [ ] Use `MERGE (n:Label {id: $id, repository_id: $repository_id}) ON CREATE SET n += $props ON MATCH SET n += $props` for nodes; `MERGE` for relationships with property merges.
- [ ] For `MODIFIED`: persist `additions`, `deletions`, `change_type`; for `CO_CHANGED_WITH`: `count`, `weight`, `confidence`; for `REVERTS`: target commit hash; for `IMPORTS`: plain edge.
- [ ] No method accepts raw Cypher from callers; forbid f-string Cypher via review.

**Test criteria:**
- Unit tests mock `session.run` and assert parameters dict contains all values and query contains `$id` placeholders.
- Passing a path with `"'` or `;--` stores literally and does not alter query semantics.

## 9. Enforce idempotent MERGE semantics for nodes and relationships

- [ ] Ensure repeated `write_commit`, `write_file`, `write_relationship` calls with identical stable IDs produce exactly one node/relationship (verify via `COUNT` queries).
- [ ] Ensure `ON MATCH SET` updates mutable properties (e.g., `Repository.current_head`, `Repository.last_ingested_at`) without duplicating.

**Test criteria:** Integration test writes the same fixture graph twice and asserts `MATCH (n) RETURN count(n)` and relationship counts are unchanged; a third write with updated `Repository.head` updates the property.

## 10. Implement batched writes with configurable batch size

- [ ] Implement `write_commits_batch(repository_id, commits, batch_size)`, `write_files_batch`, and generic `execute_batch(cypher, batch)` using `UNWIND $batch AS row MERGE ...`.
- [ ] Read `batch_size` from validated config, validate `1 <= batch_size <= 10000`, default documented.
- [ ] Each batch runs in a bounded transaction; no single transaction ingests the entire dataset.

**Test criteria:**
- Integration test with 250 commits and `batch_size=100` executes 3 transactions (spy `execute_write` call count or log).
- `batch_size=0` or negative raises validation error at config load or gateway construction.

## 11. Enforce repository isolation in every read and write

- [ ] Every node write sets `repository_id`; every read filters by `repository_id`; every relationship write validates both endpoints share the same `repository_id`.
- [ ] Gateway method signatures require `repository_id` as explicit first argument.
- [ ] Cross-repository relationship attempts raise `GraphWriteError`/`GraphValidationError` before Cypher execution.

**Test criteria:**
- Two repositories ingested with overlapping file paths; querying files for repo A returns only repo A's files.
- Attempt to create `CO_CHANGED_WITH` between files of different repositories raises a domain error and creates no relationship.

## 12. Handle file rename and identity edge cases

- [ ] When ingestion supplies a confident rename, update `File.current_path` and append old path to `past_paths`; do not create a duplicate node.
- [ ] When rename is uncertain, keep distinct nodes per PLAN identity rule.
- [ ] Document behavior in repository docstring.

**Test criteria:** Integration tests for both cases: confident rename preserves single node with updated path; uncertain rename leaves two nodes.

## 13. Provide Gateway facade and dependency-injection wiring

- [ ] Implement `GraphGateway` composing the repositories and relationship writers; expose `get_repository`, `upsert_repository`, `upsert_authors`, `upsert_commits`, `upsert_files`, `upsert_relationships`, `initialize_schema`, and `verify_connectivity`.
- [ ] Keep Cypher confined to repositories; upper layers import only `gateway` and `errors`.
- [ ] Wire gateway into FastAPI lifespan and health check (readiness probe calls `verify_connectivity()`).

**Test criteria:** A thin service test imports `GraphGateway` and exercises create→retrieve→update→idempotent re-write without touching `neo4j` driver directly.

## 14. Integration test harness against disposable Neo4j

- [ ] Add `tests/integration/test_graph_foundation.py` (or `tests/integration/graph/`) using Testcontainers or `docker compose` disposable Neo4j.
- [ ] Cover: connectivity, constraint/index presence, CRUD for each node type, idempotent duplicates, batch writes, repo isolation, session cleanup, and migration idempotency.
- [ ] Mark integration tests to allow `pytest -m "not integration"` for CI without Docker.

**Test criteria:** `pytest -m integration` passes against a fresh Neo4j; `pytest -m "not integration"` passes without Docker.

## 15. Documentation and housekeeping

- [ ] Document stable ID contract and repository isolation in `src/chronotrace/graph/README.md` or module docstrings.
- [ ] Ensure `src/chronotrace/graph/migrations/` Cypher files are included in the package (`MANIFEST.in`/`pyproject` package-data) if needed for Docker image.
- [ ] Update `docker-compose.yml` example and healthcheck notes if graph readiness probe changes.

**Test criteria:** Package build (`python -m build` or `pip install -e .`) includes migration files; `SHOW CONSTRAINTS` check passes after `pip install` path resolution.

### Completion gate (maps to PLAN § Iteration 2 Acceptance criteria)

- [ ] A single integration fixture test performs: create a repository, authors, commits, files, and all relationship types → retrieve → update one property → idempotently re-write → asserts no duplicates and correct repository scoping.
- [ ] Schema init is safe to run repeatedly; constraints and indexes exist after init.
- [ ] Database failures map to domain errors without secret leakage.
- [ ] All unit + integration graph tests pass in CI with coverage on gateway error mapping.

## Dependencies

- Requires validated configuration from Iteration 1 (graph URI/database/batch_size/env credentials).
- Blocks Iteration 3 (Ingestion) — ingestion needs gateway batch writes; Iteration 4 (Static analysis) needs `IMPORTS` persistence; Iteration 5 (Analytics) needs graph reads.

## Out of Scope

- Git history extraction, import resolution, analytics scoring, MCP tool registration, agent/model, API routes, CLI Docker commands.
