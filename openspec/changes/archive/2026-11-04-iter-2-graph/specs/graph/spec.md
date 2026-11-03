# Spec Delta

## Purpose

Provides the durable Neo4j graph foundation for ChronoTrace: shared driver lifecycle, versioned schema with constraints and indexes, and idempotent parameterized persistence with strict repository isolation.

## ADDED Requirements

### Requirement: Neo4j driver lifecycle and reuse

The system SHALL provide a shared Neo4j driver lifecycle that reuses a single application-level driver instance, exposes explicit `verify_connectivity()` and `close()` operations, and never creates a new driver per query.

#### Scenario: Driver reuse across queries

- **WHEN** the graph layer executes two consecutive queries through the shared driver
- **THEN** both queries use the same underlying driver instance and no per-query driver is created

#### Scenario: Verify connectivity before readiness

- **WHEN** application startup validates graph readiness via `verify_connectivity()`
- **THEN** the check succeeds only when Neo4j is reachable and the configured database is accessible, otherwise it raises a domain-level connection error

#### Scenario: Driver close releases resources

- **WHEN** the application shuts down and calls `close()` on the shared driver
- **THEN** the driver releases all connections and subsequent operations raise a clear not-available error without leaking driver internals

### Requirement: Session and transaction management

The system SHALL ensure every Neo4j session and transaction is closed correctly on success, on exception, and on application shutdown, using context managers or explicit finally-close semantics.

#### Scenario: Session closed after successful query

- **WHEN** a repository method completes a successful read or write
- **THEN** the underlying session and transaction are closed and no sessions remain open

#### Scenario: Session closed after failure

- **WHEN** a Cypher execution raises a transient or constraint error
- **THEN** the session is still closed, the error is mapped to a domain-level error type, and the connection remains usable for the next operation

### Requirement: Stable repository-scoped identifiers

The system SHALL define and enforce repository-scoped stable identifiers for `Repository`, `Commit`, `File`, and `Author` and use them as the sole identity for MERGE, lookup, and isolation.

#### Scenario: Repository-scoped commit identity

- **WHEN** two repositories contain a commit with the same hash
- **THEN** they are persisted as distinct `Commit` nodes keyed by `(repository_id, hash)` and queries for one repository never return the other's commit

#### Scenario: Repository-scoped file identity

- **WHEN** two repositories contain a file at the same path
- **THEN** they are persisted as distinct `File` nodes keyed by `(repository_id, path)` and file lookups are always filtered by `repository_id`

#### Scenario: Author stable identity

- **WHEN** commits from the same repository share author name and email
- **THEN** they reference the same `Author` node keyed by stable author ID within that repository scope

### Requirement: Initial node types and properties

The system SHALL persist the initial node types with the properties defined in PLAN.md Iteration 2: `Repository` (stable ID, name, remote URL, default branch, current head, last successful ingestion timestamp), `Author` (stable ID, name, email where available), `Commit` (stable ID, full hash, repository ID, date, message, fix/revert flags, changed-file count), and `File` (stable ID, repository ID, current path, historical path information as needed, alive flag, test flag, language).

#### Scenario: Repository node persisted with required fields

- **WHEN** a repository is written with all required properties
- **THEN** a `Repository` node exists with those properties and can be retrieved by stable ID

#### Scenario: Commit node preserves full hash and flags

- **WHEN** a commit with fix and revert flags is persisted
- **THEN** the `Commit` node stores the full (non-abbreviated) hash, date, message, fix flag, revert flag, and changed-file count without truncation

#### Scenario: File node stores lifecycle flags

- **WHEN** a file is written with alive, test, and language attributes
- **THEN** the `File` node stores `current_path`, `alive`, `is_test`, `language`, and retains historical path information when provided

### Requirement: Initial relationship types

The system SHALL persist the initial relationships: `Repository -[:CONTAINS]-> File`, `Commit -[:BELONGS_TO]-> Repository`, `Author -[:AUTHORED]-> Commit`, `Commit -[:MODIFIED]-> File` (with additions, deletions, change type), `Commit -[:REVERTS]-> Commit`, `File -[:IMPORTS]-> File`, and `File -[:CO_CHANGED_WITH]-> File` (with count, weight, directional confidence), each scoped to a single repository.

#### Scenario: Commit modification relationship stores diff stats

- **WHEN** a commit modifies a file with known additions, deletions, and change type
- **THEN** the `MODIFIED` relationship stores those values and unknown line statistics are stored as null rather than zero

#### Scenario: Co-change relationship stores directional confidence

- **WHEN** a co-change between two files is computed
- **THEN** the `CO_CHANGED_WITH` relationship stores `count`, `weight`, and directional `confidence` defined as proportion of changes to file A that also changed file B

#### Scenario: Revert relationship links commits

- **WHEN** a revert commit is ingested with a resolvable target commit
- **THEN** a `REVERTS` relationship is created from the revert commit to the target commit

### Requirement: File identity and rename handling

The system SHALL preserve file identity only when a rename can be established confidently from Git metadata; otherwise it SHALL NOT silently merge uncertain file identities and SHALL keep distinct nodes with preserved old and new paths.

#### Scenario: Confident rename preserves identity

- **WHEN** a rename is established confidently via Git rename detection
- **THEN** the File node's identity is preserved with updated `current_path` and retained historical path

#### Scenario: Uncertain rename does not merge

- **WHEN** a file move cannot be established confidently
- **THEN** the system keeps distinct File nodes and does not silently merge them

### Requirement: Co-change semantics and interpretation

The system SHALL document and enforce that co-change confidence is directional and that CO_CHANGED_WITH is an observed association, not proof of causation or direct dependency.

#### Scenario: Directional co-change query

- **WHEN** querying co-change confidence for file A -> B versus B -> A
- **THEN** results may differ and the query correctly returns each direction's confidence value

#### Scenario: Co-change not presented as dependency

- **WHEN** analytics or downstream consumers read co-change relationships
- **THEN** the relationship type and confidence are exposed as observed co-change evidence, never as a proven import dependency

### Requirement: Uniqueness constraints for stable identifiers

The system SHALL create uniqueness constraints for stable node identifiers and enforce them at the database level.

#### Scenario: Constraints present after initialization

- **WHEN** schema initialization completes on a fresh database
- **THEN** uniqueness constraints exist for `Repository.id`, `Author.id`, `Commit.id`, and `File.id` (repository-scoped stable IDs)

#### Scenario: Duplicate node creation is prevented

- **WHEN** two writes attempt to create the same stable node identifier
- **THEN** only one node exists and no constraint violation is leaked as a raw driver error

### Requirement: Indexes for frequently queried fields

The system SHALL create indexes for frequently queried fields including `Commit.hash`, `File.path`, `File.is_test`, `Commit.date`, and `Repository.name` to support ingestion and analytics query patterns.

#### Scenario: Indexes present after initialization

- **WHEN** schema initialization completes
- **THEN** indexes exist for `Commit(hash)`, `File(path)`, `File(is_test)`, `Commit(date)`, and `Repository(name)` and are used for parameterized queries

### Requirement: Version-controlled Cypher migrations and tracking

The system SHALL store all schema statements as version-controlled Cypher migration files under `src/chronotrace/graph/migrations/` (or equivalent) and implement a migration/version-tracking mechanism that records applied versions in the database.

#### Scenario: Migrations are version-controlled files

- **WHEN** schema changes are introduced
- **THEN** they appear as numbered Cypher files in the migrations directory and are the sole source of schema DDL

#### Scenario: Migration runner records applied versions

- **WHEN** the migration runner executes
- **THEN** it records each applied migration version in a `SchemaMigration` or equivalent node and skips already-applied versions on subsequent runs

### Requirement: Idempotent schema initialization and migrations

The system SHALL make schema initialization and migration execution safe to run repeatedly; re-running MUST NOT error, duplicate constraints, or duplicate data.

#### Scenario: Repeated initialization is safe

- **WHEN** schema initialization is executed twice consecutively on the same database
- **THEN** the second run succeeds with no errors and leaves constraints and indexes unchanged

#### Scenario: Re-running migrations skips applied versions

- **WHEN** migrations have already been applied and the runner is invoked again
- **THEN** no new DDL is executed and the version tracking state remains consistent

### Requirement: Parameterized Cypher for all reads and writes

The system SHALL use parameterized Cypher for every read and write and MUST NOT interpolate values via string concatenation or expose arbitrary Cypher execution to callers.

#### Scenario: Parameterized write

- **WHEN** a repository method writes a node property containing special characters (e.g., quotes, semicolons, Cypher keywords)
- **THEN** the value is passed as a Cypher parameter and stored literally without injection or query failure

#### Scenario: No arbitrary Cypher endpoint

- **WHEN** a caller attempts to execute arbitrary Cypher through the graph layer
- **THEN** no public method accepts raw Cypher strings; only validated repository methods with fixed query templates are available

### Requirement: Idempotent writes via MERGE on stable identifiers

The system SHALL use `MERGE` (or equivalent idempotent operation) on stable identifiers for all core nodes and relationships so that repeated writes do not create duplicates.

#### Scenario: Repeated node write does not duplicate

- **WHEN** the same `Commit` node is written twice with identical stable ID
- **THEN** only one `Commit` node exists after the second write

#### Scenario: Repeated relationship write does not duplicate

- **WHEN** the same `MODIFIED` or `CO_CHANGED_WITH` relationship is written twice
- **THEN** only one relationship exists with the latest properties merged

### Requirement: Batch writes with configurable batch size

The system SHALL support batched writes with a configurable batch size sourced from validated configuration and execute writes in bounded batches within transactions.

#### Scenario: Batch size respected

- **WHEN** persisting 250 commits with a configured batch size of 100
- **THEN** the writer executes three batches (100, 100, 50) without loading all operations into a single unbounded transaction

#### Scenario: Batch failure maps to domain error

- **WHEN** a batch write fails due to a transient database error
- **THEN** the error is surfaced as a domain-level `GraphWriteError` with a non-secret message and no partial-batch silent success

### Requirement: Repository isolation on all graph operations

The system SHALL ensure repository isolation so that records from separate repositories cannot be accidentally mixed; every node carries `repository_id` and every query filters by it.

#### Scenario: Cross-repository read isolation

- **WHEN** querying files for repository A
- **THEN** the result set contains no files belonging to repository B even when paths overlap

#### Scenario: Cross-repository write isolation

- **WHEN** writing a co-change or import relationship
- **THEN** the write validates that both endpoints belong to the same repository and rejects cross-repository endpoints

### Requirement: Domain-level error handling for database failures

The system SHALL surface database failures through clear domain-level errors (e.g., `GraphConnectionError`, `GraphWriteError`, `GraphNotAvailable`) without leaking driver-specific exceptions or secrets in messages.

#### Scenario: Database unavailable maps to domain error

- **WHEN** Neo4j is unreachable during a query
- **THEN** the caller receives a `GraphConnectionError` or `GraphNotAvailable` with an actionable message and no credential or stack internals

#### Scenario: Constraint violation maps to domain error

- **WHEN** a write violates a schema constraint due to application logic error
- **THEN** the caller receives a domain-level write error that describes the invariant violated without exposing raw Cypher or driver state

### Requirement: Graph repositories and persistence isolation

The system SHALL provide graph repositories or focused persistence modules per core entity (Repository, Author, Commit, File, and relationship writers) behind a narrow gateway facade, keeping Cypher confined to the graph layer and making repositories independently testable.

#### Scenario: Repository isolation via gateway

- **WHEN** ingestion or analytics needs to persist commits and files
- **THEN** it calls the graph gateway/repository interface and never executes Cypher directly

#### Scenario: Repository unit testability with fake or test double

- **WHEN** a repository is tested without a live Neo4j instance
- **THEN** its interface allows substitution with a fake or in-memory double for unit tests while integration tests run against a disposable Neo4j container
