# Spec Delta

## Purpose

Repository acquisition and Git history ingestion that turns a remote Git URL into repository-scoped graph evidence without executing repository code, supporting filtering, rename handling, and retryable incremental writes.

## ADDED Requirements

### Requirement: Repository URL validation and canonical identity

The system SHALL accept a remote Git URL, validate supported transport formats (https, http, ssh / `git@host:`, file), reject unsupported schemes with an actionable error, and derive a stable canonical repository identity by normalizing the URL (strip trailing `.git` and slashes, lowercase host, remove embedded credentials, canonicalize `git@host:owner/repo` to `ssh://` form) and persisting a generated stable repositoryId bound to that canonical URL.

#### Scenario: Valid HTTPS URL accepted

- **WHEN** a user requests ingestion with `https://github.com/org/repo.git`
- **THEN** the system accepts the URL and derives the canonical identity `https://github.com/org/repo`

#### Scenario: SSH scp-like URL accepted

- **WHEN** a user requests ingestion with `git@github.com:org/repo.git`
- **THEN** the system accepts the URL and canonicalizes it to `ssh://github.com/org/repo`

#### Scenario: Unsupported transport rejected

- **WHEN** a user requests ingestion with `ftp://example.com/repo.git`
- **THEN** the system rejects the request with an error naming the unsupported scheme and listing supported formats

#### Scenario: Canonical identity stable across variants

- **WHEN** ingestion is requested twice for `https://github.com/Org/Repo.git` and `https://github.com/org/repo`
- **THEN** the system resolves both to the same repositoryId and does not create a duplicate Repository node

### Requirement: Repository clone and fetch acquisition

The system SHALL clone a repository into the configured managed repositories directory when no local copy exists, and fetch updates (with prune) when a local copy already exists, recording the selected branch and checked-out head commit without executing repository code.

#### Scenario: Initial clone

- **WHEN** ingestion is requested for a URL whose local directory does not exist
- **THEN** the system clones the repository into `<repositories_dir>/<derived-slug>` and records the default or requested branch and its HEAD hash

#### Scenario: Existing repository fetch

- **WHEN** ingestion is requested for a URL whose local directory already exists
- **THEN** the system runs `git fetch --all --prune` and fast-forwards the selected branch to the remote head without re-cloning

#### Scenario: No repository code execution

- **WHEN** acquisition runs
- **THEN** the system does not execute hooks, build scripts, or any file from the repository (e.g., no `python` invocation, `--no-checkout` handling or inert checkout, and `GIT_TERMINAL_PROMPT=0`)

### Requirement: Git history extraction via PyDriller

The system SHALL use PyDriller to traverse commit history in chronological order and for each commit capture full hash, author name and email where available, commit date, message, changed paths, change type (ADD, MODIFY, DELETE, RENAME, COPY), and additions/deletions counts, preserving unknown line statistics as null rather than zero.

#### Scenario: Full commit metadata captured

- **WHEN** PyDriller yields a commit modifying `src/foo.py`
- **THEN** the system captures hash, author identity, date, message, path, change type, and line counts for that modification

#### Scenario: Unknown line stats preserved as null

- **WHEN** PyDriller reports `added_lines` or `deleted_lines` as unavailable for a binary or merge commit
- **THEN** the system stores the value as null and does not coerce it to zero

#### Scenario: Chronological order

- **WHEN** a repository contains commits A (older) and B (newer)
- **THEN** the system processes A before B

### Requirement: Path normalization and exclusion filtering

The system SHALL normalize all file paths to POSIX form (`/` separator) consistently across operating systems and apply configurable glob-based path exclusion patterns, testing matching against both POSIX and Windows-style inputs, ensuring excluded paths do not enter the graph.

#### Scenario: POSIX normalization

- **WHEN** a modification reports path `src\\utils\\helper.py` on Windows
- **THEN** the system normalizes it to `src/utils/helper.py` before filtering and persistence

#### Scenario: Glob exclusion

- **WHEN** config excludes `**/__pycache__/**` and `**/*.pyc` and a commit modifies `src/__pycache__/cache.pyc`
- **THEN** the system excludes that path from graph writes and increments the filtered-out counter

#### Scenario: Windows-style glob matching

- **WHEN** an exclusion pattern is evaluated against `src\\tests\\fixtures\\data.json`
- **THEN** the system matches it identically to `src/tests/fixtures/data.json`

### Requirement: File change type and rename handling

The system SHALL handle file additions, modifications, deletions, copies, and renames; preserve old and new paths for rename/copy evidence; and preserve file identity only when a rename can be established confidently, without silently merging uncertain file identities.

#### Scenario: Rename with confidence preserves identity

- **WHEN** PyDriller reports a RENAME from `src/old.py` to `src/new.py` with high similarity
- **THEN** the system updates the existing File node's current path to `src/new.py` and preserves both old and new paths in the modification edge evidence

#### Scenario: Uncertain rename does not merge identities

- **WHEN** a rename similarity is below the confidence threshold or PyDriller reports a DELETE plus ADD without rename metadata
- **THEN** the system creates a distinct File node for the new path and marks the old path's File as not alive, without merging identities

#### Scenario: Deletion marks file as not alive

- **WHEN** a commit deletes `src/removed.py`
- **THEN** the system retains the File node, sets `alive = false`, and records the deletion modification edge

#### Scenario: Copy creates distinct file

- **WHEN** a commit copies `src/a.py` to `src/b.py`
- **THEN** the system creates a new File node for `src/b.py` and does not merge it with `src/a.py`

### Requirement: Bug-fix commit labeling

The system SHALL label likely bug-fix commits by applying configurable case-insensitive regex patterns to commit messages and, on match, set `isFix = true` and store the heuristic reason (matched pattern and message excerpt).

#### Scenario: Fix pattern matches

- **WHEN** a commit message is `Fix null pointer when parsing config`
- **THEN** the system labels the commit with `isFix = true` and `fixReason` containing the matched pattern `fix` and an excerpt

#### Scenario: Case-insensitive matching

- **WHEN** a commit message is `HOTFIX: address race condition`
- **THEN** the system matches pattern `hotfix` case-insensitively and labels it as a fix

#### Scenario: Non-fix not labeled

- **WHEN** a commit message is `Add new analytics dashboard`
- **THEN** the system leaves `isFix = false` and does not set a fix reason

### Requirement: Revert detection and linking

The system SHALL detect revert commits via message patterns (e.g., `Revert "..."` and `This reverts commit <hash>`) and, when the target hash can be resolved within the repository, create a `REVERTS` relationship from the revert commit to its target commit.

#### Scenario: Revert linked when target exists

- **WHEN** a commit message is `Revert "Add feature X" This reverts commit abc123def456...` and hash `abc123def456` exists in the ingested history
- **THEN** the system creates a `(:Commit {hash: revertHash})-[:REVERTS]->(:Commit {hash: abc123def456})` relationship

#### Scenario: Revert without resolvable target not linked

- **WHEN** a commit message matches the revert pattern but the referenced hash does not exist in the repository
- **THEN** the system does not create a REVERTS relationship and records that the target was unresolvable

#### Scenario: Non-revert not linked

- **WHEN** a commit message does not match the revert pattern
- **THEN** the system does not create a REVERTS relationship

### Requirement: Ingestion status lifecycle

The system SHALL persist an explicit ingestion status with states `PENDING`, `CLONING`, `EXTRACTING`, `FILTERING`, `WRITING_GRAPH`, `COMPLETED`, `FAILED`, along with stage timestamps, counters, and error summaries, transitioning monotonically and recording failure details when ingestion fails.

#### Scenario: Successful lifecycle

- **WHEN** ingestion completes without error
- **THEN** the system records transitions through `PENDING → CLONING → EXTRACTING → FILTERING → WRITING_GRAPH → COMPLETED` with timestamps for each stage

#### Scenario: Failed stage recorded

- **WHEN** cloning fails due to network error
- **THEN** the system sets status to `FAILED`, preserves the stage `CLONING` timestamp, and stores an error summary without leaking credentials

#### Scenario: Status queryable

- **WHEN** a caller queries ingestion status for a repository
- **THEN** the system returns the current state, stage timestamps, counters, and last error summary if any

### Requirement: Incremental ingestion

The system SHALL persist the last successfully ingested head commit and ingestion timestamp, fetch the remote, identify commits not previously ingested, process new commits in chronological order, and update the repository head and successful ingestion timestamp only after all required stages succeed.

#### Scenario: Incremental processes only new commits

- **WHEN** a repository was previously ingested up to commit `H1` and the remote now has `H2` and `H3` after `H1`
- **THEN** the system fetches, identifies `H2` and `H3` as new, and processes only those commits in order `H2` then `H3`

#### Scenario: Successful incremental updates persisted head

- **WHEN** incremental ingestion of `H2` and `H3` completes successfully
- **THEN** the system updates `Repository.lastIngestedHead` to `H3` and `lastSuccessfulIngestionAt` to the completion time

#### Scenario: Failed ingestion does not advance head

- **WHEN** ingestion fails during `WRITING_GRAPH`
- **THEN** the system leaves `lastIngestedHead` at its prior value `H1` and does not update `lastSuccessfulIngestionAt`

### Requirement: Rewritten history detection

The system SHALL detect rewritten history or force-pushes by checking whether the previously ingested head is an ancestor of the current remote head, and for the MVP require a full re-ingestion rather than attempting unsafe reconciliation when the check fails.

#### Scenario: Force-push detected

- **WHEN** the stored `lastIngestedHead` is not an ancestor of the fetched remote HEAD (e.g., `git merge-base --is-ancestor` fails)
- **THEN** the system marks ingestion as `FAILED` with reason `needsFullReingestion` and does not partially ingest the divergent history

#### Scenario: Normal fast-forward not flagged

- **WHEN** `lastIngestedHead` is an ancestor of the remote HEAD
- **THEN** the system proceeds with incremental ingestion

### Requirement: Retryable and idempotent batched writes

The system SHALL perform graph writes using batched, parameterised Cypher with `MERGE` on stable repository-scoped identifiers (`repositoryId + commit hash`, `repositoryId + file path`), configured batch size, and correct session/transaction lifecycle, so that repeated ingestion of the same commits does not duplicate nodes or relationships and supports recomputation of co-change edges.

#### Scenario: Repeated ingestion does not duplicate

- **WHEN** the same commit history is ingested twice
- **THEN** no duplicate `Commit`, `File`, `Author`, or `MODIFIED` / `AUTHORED` relationships are created

#### Scenario: Batched parameterised writes

- **WHEN** 500 commits are ingested with `batch_size = 100`
- **THEN** the system issues five `UNWIND $batch AS row MERGE ...` parameterised statements and closes each session/transaction correctly

#### Scenario: Repository isolation

- **WHEN** two repositories contain a file with the same path `src/main.py`
- **THEN** the system stores distinct File nodes scoped by `repositoryId` and does not collide them

#### Scenario: Co-change recomputation

- **WHEN** incremental ingestion adds new commits that co-modify files A and B
- **THEN** the system recomputes or incrementally rebuilds co-change edges for the affected repository without duplicating prior counts

### Requirement: Ingestion observability

The system SHALL expose ingestion counters (commits processed, files created/updated, excluded paths, batches written) and error summaries for monitoring progress of batch writes and overall ingestion.

#### Scenario: Counters observable

- **WHEN** ingestion processes 120 commits with 15 paths excluded in 3 batches
- **THEN** the system reports `commits: 120, filteredOut: 15, batches: 3` alongside the ingestion status

#### Scenario: Error summary on failure

- **WHEN** ingestion fails with a Git fetch error
- **THEN** the system stores an error summary indicating the failed stage and the error class without exposing secret values

