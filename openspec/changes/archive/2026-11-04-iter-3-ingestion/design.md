# Design

## Context

Iteration 2 provides Neo4j driver reuse, graph schema, uniqueness constraints, indexes, and `MERGE`-based idempotent repositories for `Repository`, `Author`, `Commit`, and `File` nodes. The graph is empty. Iteration 1 provides validated configuration (`chronotrace.yml` via Pydantic v2, env secrets outside YAML) including `repositories_dir`, analysis/history/filtering settings, bug-fix label patterns, and `graph.batch_size`. This design covers how to turn a remote Git URL into that historical graph without executing repository code, honoring idempotency, retryability, and repository isolation. See `proposal.md` for motivation and `specs/ingestion/spec.md` for normative requirements.

## Goals / Non-Goals

**Goals:**

- Provide a single orchestrated ingestion pipeline with explicit lifecycle states and observable progress.
- Ensure every write is idempotent, parameterised, and repository-isolated via stable identifiers.
- Support incremental ingestion (only new commits) and safe handling of rewritten history.
- Preserve file identity semantics for renames/copies without silent merges.

**Non-Goals:**

- Static import graph extraction (Iteration 4) and analytics/MCP/agent/API layers (Iterations 5+).
- Arbitrary Cypher execution or user-supplied Cypher.
- Reconciliation of force-pushed history beyond detection + full re-ingestion.
- Cloning private hosts requiring custom credential helpers beyond standard Git credential resolution.

## Decisions

### 1. Acquisition layer: Git CLI primary, library fallback

- **Decision:** Use Git CLI (`git clone`, `git fetch`, `git rev-parse`) as the primary acquisition mechanism; optionally wrap with a small Python helper rather than a heavy Git library.
- **Rationale:** Git CLI is universally available, handles all transport formats (https/ssh/file), credential helpers, and shallow/partial nuances faithfully. PyDriller itself shells out to Git.
- **Alternatives considered:** Pure `GitPython` / `dulwich` — tighter Python control but diverges from PyDriller's traversal and adds transport/credential edge cases. Rejected as primary.

### 2. Canonical repository identity

- **Decision:** Normalize remote URL: strip trailing `.git` and `/`, lowercase host, remove embedded credentials, canonicalize `git@host:owner/repo` to `ssh://host/owner/repo`. Derive local directory as `<repositories_dir>/<hash-of-canonical>` or slugified name with collision suffix. Persist canonical URL as `Repository.remoteUrl` and generated stable `repositoryId` (UUID or hash) used as FK in all nodes/relationships.
- **Rationale:** URLs have many equivalent forms; stable graph identity must survive URL variant reuse across ingestions.
- **Alternatives considered:** Raw URL as ID — collides on variants and leaks credentials.

### 3. Extraction via PyDriller

- **Decision:** Traverse with `pydriller.Repository(path, order="date-order", only_in_branch=..., from_commit=lastHead)` or manual `git log` to respect incremental range; for each commit capture `hash`, `author.name/email`, `committer` separately if divergence matters, `author_date`, `msg`, `modifications` (path, old_path, change_type, added_lines, deleted_lines, complexity). Preserve `added_lines`/`deleted_lines` as `null` when PyDriller reports unavailable rather than coalescing to 0.
- **Rationale:** PyDriller normalizes diff parsing, rename detection, and change-type mapping already required by PLAN.md; manual `git log --numstat` reimplements it poorly.
- **Alternatives considered:** Direct `git log` plumbing only — rejected as duplicate effort.

### 4. Path normalization and filtering

- **Decision:** Normalize all paths to POSIX (`/`), lowercase only for matching on Windows if needed, but store canonical POSIX form. Apply exclusion as glob patterns (e.g., `**/__pycache__/**`, `**/*.pyc`, `tests/fixtures/**`) matched via `pathspec`/`fnmatch` with `pathlib.PurePosixPath.match`. Test both POSIX and Windows-style separators by normalizing before match.
- **Rationale:** Repositories checked out on heterogeneous OSes must filter identically; graph must store one canonical form.
- **Alternatives considered:** Regex-only filtering — less ergonomic for users; glob aligns with typical ignore patterns.

### 5. Rename and copy semantics

- **Decision:** Preserve PyDriller `change_type` (ADD, MODIFY, DELETE, RENAME, COPY) plus `old_path`/`new_path`. File node identity = `(repositoryId, currentPath)` per Iteration 2, but rename creates evidence: do not merge two unrelated paths solely on rename similarity. If PyDriller reports RENAME with high similarity, update `File.currentPath` and keep historical path in relationship log; otherwise create new File node for the new path and mark old path's File as not alive. Copy creates a new File node linked by co-change/shared-origin metadata, not by identity merge.
- **Rationale:** Satisfies "preserve identity only when established confidently; do not silently merge uncertain identities."
- **Alternatives considered:** Always merge rename — violates data integrity on false positives.

### 6. Fix and revert labeling

- **Decision:** Case-insensitive regex list from config (`fixLabelPatterns`, e.g., `fix`, `bugfix`, `hotfix`, `fixes #\d+`) applied to commit message. On match, set `Commit.isFix = true` and `fixReason = matchedPattern + excerpt`. Revert detection: pattern `^Revert ".*"` and `This reverts commit ([0-9a-f]{7,40})`; extract hash, resolve via in-memory set of ingested hashes or graph lookup; create `(:Commit)-[:REVERTS]->(:Commit)` only when target exists.
- **Rationale:** Heuristic, not proof — matches PLAN.md's "likely fix" and revert-link-only-when-resolvable semantics.
- **Alternatives considered:** ML classifier — out of scope for deterministic MVP.

### 7. Status machine and incremental ingestion

- **Decision:** Persist `IngestionStatus` as property on `Repository` or dedicated `:IngestionRun` node with states `PENDING → CLONING → EXTRACTING → FILTERING → WRITING_GRAPH → COMPLETED` (and `FAILED` from any state). Store `stageTimestamps`, `counters {commits, files, filteredOut, batches}`, `errorSummary`. Incremental: read `Repository.lastIngestedHead`; `git fetch --all --prune`; compute `git rev-list lastHead..HEAD --count` and `merge-base --is-ancestor lastHead HEAD` to detect rewritten history; if not ancestor → mark `FAILED` with `needsFullReingestion` flag. Otherwise traverse only `from_commit=lastHead` (exclusive) in chronological order; update `lastIngestedHead` and `lastSuccessfulIngestionAt` only after `WRITING_GRAPH` commits successfully.
- **Rationale:** Cheap ancestor check detects force-push; updating head only on success preserves retryability.
- **Alternatives considered:** Re-traverse full history each time — wasteful; unsafe reconciliation of rewritten history — out of scope.

### 8. Idempotent batch writes

- **Decision:** All writes use `UNWIND $batch AS row MERGE ... ON CREATE SET ... ON MATCH SET` with repository-scoped keys (`repositoryId + commitHash`, `repositoryId + filePath`). Batch size from `config.graph.batch_size`. Use driver session per batch, explicit transaction, `try/finally` close. Co-change edges computed per commit via frequency maps then `MERGE (a:File)-[r:CO_CHANGED_WITH]->(b:File) ON CREATE SET r.count=1 ... ON MATCH SET r.count = r.count + 1` or rebuilt batch-wise.
- **Rationale:** `MERGE` + stable IDs gives idempotency; batched UNWIND is the idiomatic Neo4j bulk pattern; parameterized Cypher satisfies security constraint.
- **Alternatives considered:** Per-row queries — slower and harder to make transactional.

## Risks / Trade-offs

- [Risk] Large repository clone/extract takes minutes and may exceed memory if commits are buffered entirely → Mitigation: stream PyDriller iterator, flush batches eagerly, expose progress counters, document timeout expectations.
- [Risk] PyDriller rename similarity threshold misclassifies rename → Mitigation: conservative threshold, preserve old_path/new_path evidence, do not merge on weak signal; log copy/rename decisions.
- [Risk] Force-pushed history leaves graph stale if only incremental path is followed → Mitigation: ancestor check fails fast, surface `needsFullReingestion`, never auto-delete until user confirms full re-ingestion.
- [Risk] Glob exclusion silently drops evidence if patterns are overly broad → Mitigation: log filtered paths count and sample, validate patterns at config load, test against POSIX + Windows paths.
- [Risk] Concurrent ingestion runs race on same repository → Mitigation: per-repository file lock or `Repository.ingestionLock` property with CAS via Cypher, fail second caller with actionable error.
- [Risk] Unknown line stats conflated with zero → Mitigation: store as null and document distinction; tests assert null preservation.
- [Risk] Git credential prompts hang CI → Mitigation: set `GIT_TERMINAL_PROMPT=0`, fail fast with actionable message, never log credentials.

## Migration Plan

- No schema migration required beyond Iteration 2 baseline; if Iteration 2 migrations are pending, run them before first ingestion.
- Deployment: ship as part of `agent-api` image; no new Compose service. Run `ingest <url> --branch <branch>` or API-triggered ingestion; on boards without graph head, ingestion starts at `PENDING`.
- Rollback: ingestion is additive and idempotent; rolling back the code does not require graph rollback. To clear a repository's graph data, delete nodes/relationships scoped by `repositoryId`.
- Re-ingestion for rewritten history: advise `ingest --full` (or API flag) that drops and rebuilds that repository's commits/files/co-change edges while preserving `Repository` node.

## Open Questions

- None that would change specs, approach, or task breakdown. Tuning questions (exact default `batch_size`, default `fixLabelPatterns` list, whether to expose `max_commits` traversal limit) are resolved at implementation from Iteration 1 config defaults and can be adjusted without spec change.

