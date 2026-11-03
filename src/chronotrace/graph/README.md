# ChronoTrace Graph Foundation

Neo4j driver lifecycle, schema migrations, and repository-isolated persistence.

## Stable ID Contract

All nodes and relationships are scoped to a single repository via `repository_id`.

| Entity | Stable ID derivation | Uniqueness |
|--------|----------------------|------------|
| `Repository` | `canonical_repository_id(remote_url)` — lowercased, trimmed `.git` and trailing `/`, or explicit `id` | `Repository.id` unique |
| `Author` | `hash(repository_id + lower(email│name))` → `"{repo}:author:{sha16}"` | `Author.id` unique |
| `Commit` | `"{repository_id}:{hash}"` (hash lowercased) | `Commit.id` unique; `Commit.hash` indexed |
| `File` | `"{repository_id}:{posix_path}"` with `path` normalized via `posixpath.normpath` and `\` → `/` | `File.id` unique; `File.path` indexed |

Idempotent writes use `MERGE (n:Label {id: $id, repository_id: $repoId}) ON CREATE SET ... ON MATCH SET ...`.
Repeated writes never create duplicates.

## Repository Isolation

- Every node **stores** `repository_id`; every read **filters** by `repository_id`.
- All public gateway/repository methods require `repository_id` as the first argument or as part of the entity.
- Relationship writers validate that endpoints share the same `repository_id` before Cypher execution and raise `GraphValidationError` for cross-repo attempts.
- Gateway signatures enforce isolation: `upsert_commits(repository_id, commits)`, `list_files(repository_id)`, etc.

## File Rename Handling

- **Confident rename** (Git rename detection — owned by iter-3): call `File.with_rename(new_path, confident=True)` or `gateway.rename_file_confident(existing_file, new_path)`. Preserves node identity (same `id`), updates `path`/`current_path`, appends old path to `past_paths`.
- **Uncertain move**: keep distinct nodes per `PLAN.md` identity rule — create a new `File` node for the new path; do not merge.

## Batch Writes

- Bounded batches via `UNWIND $batch AS row MERGE ...` per transaction.
- `batch_size` from validated `ChronoTraceConfig.graph.batch_size` (1..10 000), default 500.
- Each batch runs in `session.execute_write(...)`; no single transaction holds the full dataset.

## Co-Change Semantics

- `CO_CHANGED_WITH` stores `count`, `weight`, `confidence` (directional: `confidence(A→B) = |A∩B|/|A|`, may differ from `B→A`).
- It is an **observed association**, not proof of causation or import dependency. Consumers must not present it as a dependency.

## Sessions & Transactions

- All operations use `with driver.session(database=...) as session:` and `session.execute_write` / `execute_read`.
- No long-lived sessions; no session held as instance state; context managers guarantee close on success or failure.

## Migrations

- Versioned Cypher files under `src/chronotrace/graph/migrations/`:
  - `V001__initial_constraints.cypher` — uniqueness constraints (`IF NOT EXISTS`)
  - `V002__initial_indexes.cypher` — indexes for `Commit.hash`, `File.path`, `File.is_test`, `Commit.date`, `Repository.name` (+ repository_id helpers)
  - `V003__schema_documentation.cypher` — `SchemaMigration` uniqueness + documentation node, co-change semantics
- `MigrationRunner` / `SchemaManager` discovers `V*.cypher` in lexicographic order, tracks `SchemaMigration {version, checksum, applied_at, filename}` nodes, skips applied versions, fails on checksum mismatch (`GraphMigrationError`), and is idempotent.

## Error Handling

- All Neo4j exceptions are mapped to domain errors: `GraphConnectionError`, `GraphNotAvailable`, `GraphWriteError`, `GraphValidationError`, `GraphMigrationError`. Callers never import `neo4j.exceptions`.
- Messages never leak credentials or raw connection strings.

## Configuration

Graph settings are consumed from the validated `ChronoTraceConfig` object (iter-1). Credentials come from env (`NEO4J_USERNAME` / `NEO4J_PASSWORD` / `NEO4J_AUTH`) without logging and without re-parsing YAML. The graph layer never reads `chronotrace.yml` directly.
