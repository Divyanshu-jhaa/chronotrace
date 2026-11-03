# Tasks

## 1. Scaffolding and configuration grounding

- [ ] 1.1 Create `src/chronotrace/ingestion/` package with `__init__.py`, `acquisition.py`, `extractor.py`, `filtering.py`, `labeling.py`, `service.py` and verify `pytest --collect-only` discovers the new modules without import errors
- [ ] 1.2 Define ingestion config models and defaults (repositories_dir, branch, exclusion globs, fix patterns, batch_size) reusing Iteration 1 validated config and verify unit test loads a sample `chronotrace.yml` and rejects an invalid regex pattern with an actionable error
- [ ] 1.3 Add ingestion domain exceptions and status enum (`PENDING` through `FAILED`) with stage timestamps container and verify unit test asserts enum values and error types

## 2. Repository identity and URL validation

- [ ] 2.1 Implement canonical URL normalization (strip `.git`/slashes, lowercase host, remove credentials, canonicalize `git@host:owner/repo` → `ssh://`) and stable `repositoryId` derivation and verify unit test asserts `https://github.com/org/repo.git`, `git@github.com:org/repo.git`, and `https://user:pass@github.com/Org/Repo` all map to the same canonical identity while `ftp://` is rejected
- [ ] 2.2 Implement local directory derivation (`<repositories_dir>/<slug-or-hash>`) with collision handling and verify unit test asserts distinct URLs yield distinct directories and repeated calls for the same canonical URL are stable
- [ ] 2.3 Document supported transport formats and canonicalization rules in `docs/ingestion.md` and verify the documented examples pass the unit test vectors

## 3. Clone and fetch acquisition

- [ ] 3.1 Implement `clone_or_fetch(url, branch)` using Git CLI with `GIT_TERMINAL_PROMPT=0`, cloning when absent and `git fetch --all --prune` plus fast-forward when present, without executing repository code, and verify integration test clones a local `file://` fixture repository and re-fetches after adding a new commit
- [ ] 3.2 Record selected branch, checked-out HEAD hash, and remote default branch on the `Repository` node and verify integration test asserts `currentHead` matches `git rev-parse HEAD` after clone and after fetch
- [ ] 3.3 Handle acquisition failures with actionable errors (network, auth, invalid branch) without leaking credentials and verify unit test asserts error messages hide passwords and name the failed stage `CLONING`

## 4. PyDriller history extraction

- [ ] 4.1 Implement chronological PyDriller traversal (`order="date-order"`, filter by branch, optional `from_commit`) capturing hash, author name/email, committer date, message, and per-modification `path`, `old_path`, `change_type`, `added_lines`, `deleted_lines` and verify unit test with a temporary Git fixture extracts two commits in order with correct hashes and messages
- [ ] 4.2 Preserve unknown line statistics as `null` instead of zero and verify unit test asserts a binary-file modification or merge commit yields `added_lines is None` and `deleted_lines is None`
- [ ] 4.3 Handle empty repositories, shallow history, and malformed metadata (missing author email, empty message) gracefully and verify tests for an empty repo (no commits) and a commit with empty author email do not raise unhandled exceptions

## 5. Path normalization and exclusion filtering

- [ ] 5.1 Implement POSIX path normalization for all extracted paths and verify unit test asserts `src\\utils\\helper.py` normalizes to `src/utils/helper.py`
- [ ] 5.2 Implement glob-based exclusion matching via `PurePosixPath` / `pathspec` that tests both POSIX and Windows-style separators and verify unit test asserts `**/__pycache__/**` and `**/*.pyc` exclude `src/__pycache__/cache.pyc` while `src/main.py` passes, for both separator styles
- [ ] 5.3 Wire filtering into the pipeline so excluded paths do not reach graph writes and increment `filteredOut` counter and verify integration test ingests a fixture with one excluded file and asserts that file has no `File` node in Neo4j but the counter reflects it

## 6. Change-type and rename handling

- [ ] 6.1 Implement ADD/MODIFY/DELETE handling with `alive` flag management and verify integration test asserts after a DELETE commit the File node remains with `alive = false` and a MODIFY preserves `alive = true`
- [ ] 6.2 Implement RENAME handling preserving old/new path evidence with confidence threshold — high similarity updates `File.currentPath`, low similarity creates a new node and marks old as not alive — and verify tests cover both a confident rename fixture and an uncertain DELETE+ADD pair that does not merge identities
- [ ] 6.3 Implement COPY handling creating a distinct File node without merging and verify test asserts `src/a.py` copied to `src/b.py` yields two File nodes with distinct paths under the same repositoryId
- [ ] 6.4 Persist modification edges with `changeType`, `oldPath`, `newPath`, `additions`, `deletions` on `(:Commit)-[:MODIFIED]->(:File)` and verify Cypher query returns expected properties for each change type

## 7. Fix and revert labeling

- [ ] 7.1 Implement fix-labeling via configurable case-insensitive regex patterns storing `isFix` and `fixReason` (matched pattern + excerpt) and verify unit test asserts `Fix: ...`, `HOTFIX: ...`, and `fixes #123` are labeled while `Add feature` is not, and `fixReason` is persisted
- [ ] 7.2 Implement revert detection (`Revert "..."` and `This reverts commit <hash>`) and resolve target hash against ingested set / graph lookup to create `(:Commit)-[:REVERTS]->(:Commit)` only when resolvable and verify test asserts a revert fixture creates the relationship and an unresolvable hash does not
- [ ] 7.3 Add tests for mixed fix+revert commit (e.g., `Fix ... This reverts commit ...`) and verify both flags/relationships are applied correctly

## 8. Idempotent batched graph writes

- [ ] 8.1 Implement batched `UNWIND $batch AS row MERGE` writes for `Author`, `Commit`, `File`, and `MODIFIED`/`AUTHORED` relationships using repository-scoped keys (`repositoryId+hash`, `repositoryId+path`) and `batch_size` from config, with parameterised Cypher and correct session/transaction close, and verify integration test with `batch_size=2` ingesting 5 commits issues three batches and Neo4j contains the expected nodes
- [ ] 8.2 Verify idempotency: re-run ingestion on the same fixture repository and assert no duplicate `Commit`, `File`, or `Author` nodes and counts are unchanged (query `MATCH (c:Commit {repositoryId:$id}) RETURN count(c)`)
- [ ] 8.3 Verify repository isolation: ingest two repositories each containing `src/main.py` and assert two distinct `File` nodes exist scoped by different `repositoryId` and queries filtered by one do not return the other's data
- [ ] 8.4 Implement co-change edge recomputation / incremental rebuild for the ingested repository and verify integration test asserts `(:File)-[:CO_CHANGED_WITH {count, weight}]` edges exist for files co-modified in the same commit and are not duplicated on re-ingestion

## 9. Status lifecycle, incremental ingestion, and history handling

- [ ] 9.1 Implement ingestion status machine `PENDING → CLONING → EXTRACTING → FILTERING → WRITING_GRAPH → COMPLETED` (and `FAILED` from any state) with stage timestamps, counters (`commits`, `files`, `filteredOut`, `batches`), and error summary persistence (on Repository node or separate `IngestionRun` node) and verify integration test asserts timestamps exist after a successful run and status query returns them
- [ ] 9.2 Implement `FAILED` handling that records stage and error class without leaking secrets and does not update `lastIngestedHead` / `lastSuccessfulIngestionAt` and verify test simulates a fetch failure and asserts head is unchanged and status is `FAILED`
- [ ] 9.3 Implement incremental ingestion: persist `lastIngestedHead`, on re-run `git fetch` and compute `from_commit = lastIngestedHead`, process only new commits chronologically, update `lastIngestedHead` and `lastSuccessfulIngestionAt` only after `WRITING_GRAPH` succeeds, and verify test ingests H1 then adds H2/H3 and asserts only H2/H3 are processed and head moves to H3
- [ ] 9.4 Implement rewritten-history detection via `git merge-base --is-ancestor lastHead HEAD` and require full re-ingestion flag (`needsFullReingestion`) instead of partial reconciliation and verify test force-pushes a divergent history and asserts ingestion fails with `needsFullReingestion`
- [ ] 9.5 Implement retryability: a failed ingestion can be retried from `PENDING` and succeeds on transient error recovery and verify test fails once (e.g., mocked batch error) then retries and completes without duplicates

## 10. Integration, performance, and documentation

- [ ] 10.1 End-to-end integration test cloning and ingesting a small public Python fixture repository (e.g., `https://github.com/psf/requests` shallow snapshot or a local mirror) and verify commit, author, file, and modification graph stored in Neo4j with correct repository scoping
- [ ] 10.2 Add progress reporting: log batch progress at `INFO` level and verify manual run shows `Writing batch 1/3` style output without exposing secrets
- [ ] 10.3 Update `README.md` and `docs/ingestion.md` with ingestion usage, config options (`repositories_dir`, `exclusion`, `fixPatterns`, `batch_size`), lifecycle states, and limitations (dynamic excludes, threshold tuning) and verify docs examples match actual config schema
- [ ] 10.4 Run `openspec validate --strict` and `pytest` for ingestion unit + integration suites and verify both pass before marking iteration complete

