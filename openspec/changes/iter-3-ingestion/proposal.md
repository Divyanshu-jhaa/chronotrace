# Proposal

## Why

ChronoTrace's product capabilities (change criticality, regression risk, historical impact) depend on historical graph data that does not yet exist. Iteration 2 provides Neo4j connectivity and schema but no populated commits, files, or co-change evidence. This iteration supplies the acquisition and ingestion pipeline that transforms a remote Git repository into repository-scoped graph evidence without executing repository code, unlocking all downstream analytics.

## What Changes

- Accept a remote Git URL, validate supported transport formats (https, http, ssh / git@, file), and derive a stable canonical repository identity (normalized remote URL) with persisted generated ID fallback.
- Clone repositories into the configured managed repositories directory; fetch and fast-forward when a repository already exists on disk.
- Record selected branch, checked-out head commit, and remote default branch; never execute repository code during acquisition.
- Use PyDriller to extract commit history and file modifications in chronological order: full commit hash, author identity (name/email), commit date, message, changed paths, change type (ADD/MODIFY/DELETE/RENAME/COPY), additions/deletions (preserving unknown as null, not zero), old/new paths for renames.
- Normalize paths consistently across POSIX and Windows; apply configurable glob-based path exclusion patterns and test matching against both path styles.
- Handle file additions, modifications, deletions, copies, and renames; preserve old/new path evidence and apply confident-rename identity preservation policy (do not silently merge uncertain identities).
- Label likely bug-fix commits via configurable case-insensitive regex patterns; store heuristic reason alongside the flag.
- Detect revert commits via message pattern (e.g., `Revert "..."` / `This reverts commit <hash>`) and link `Commit -[:REVERTS]-> Commit` when target hash resolves within the repository.
- Persist ingestion status lifecycle: `PENDING`, `CLONING`, `EXTRACTING`, `FILTERING`, `WRITING_GRAPH`, `COMPLETED`, `FAILED` with stage timestamps, counters, and error summaries.
- Implement incremental ingestion: persist last successfully ingested head commit; on re-run fetch remote, identify unseen commits, process new commits chronologically, update repository head and successful ingestion timestamp only after all stages succeed; detect rewritten history / force-push and require full re-ingestion for the MVP.
- Make ingestion retryable and idempotent: use `MERGE` on stable repository-scoped identifiers (repository + commit hash, repository + file path), batched parameterised Cypher writes with configurable batch size, and correct session/transaction lifecycle.
- Recompute or rebuild repository co-change relationships when dataset size makes it simpler/safer; surface batch progress and ingestion counters.
- Ensure a failed ingestion does not mark the repository as successfully updated and leaves the persisted head unchanged.

## Capabilities

### New Capabilities

- `ingestion`: Repository acquisition, Git history extraction, path filtering, rename handling, fix/revert labeling, ingestion lifecycle and incremental/idempotent graph writes.

### Modified Capabilities

- None — no existing shipped spec to modify (all capability paths under `openspec/specs/` are initial scaffolds; `graph` from Iteration 2 defines nodes/relationships this iteration writes to but its requirements are not changed here).

## Impact

- **New code:** `src/chronotrace/ingestion/` — `acquisition.py` (clone/fetch, URL validation, canonical identity), `extractor.py` (PyDriller traversal, metadata capture), `filtering.py` (glob exclusion, POSIX normalization), `labeling.py` (fix/revert heuristics), `service.py` / `pipeline.py` (status machine, incremental logic, batch orchestration, retry/idempotency).
- **Graph layer:** Reuses `src/chronotrace/graph/` repositories and `MERGE`-based persistence from Iteration 2; adds ingestion-specific write batches and repository-head tracking.
- **Config:** Reads `repositories_dir`, `history`/`filtering`/`graph.batch_size` from validated `chronotrace.yml` (Iteration 1); no new top-level secrets.
- **CLI/API:** Ingestion will be invoked by `agent-api` and future CLI commands; this proposal does not add user-facing CLI flags beyond internal service wiring.
- **Dependencies:** `PyDriller`, Git CLI (or maintained Git library fallback), `neo4j` driver; no new infrastructure containers.
- **Risk surface:** Large clones, shallow history, malformed commit metadata, force-pushed history, and long-running PyDriller traversals — all scoped to retryable offline ingestion before graph reads.
