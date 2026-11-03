# Ingestion — Repository Acquisition and History Extraction

Turns a remote Git URL into repository-scoped graph evidence without executing repository code.

## Supported transports

| Input | Example | Canonical | Notes |
|---|---|---|---|
| `https` | `https://github.com/org/repo.git` | `https://github.com/org/repo` | strips `.git`, lowercases host/path |
| `http` | `http://github.com/org/repo/` | `http://github.com/org/repo` | same as https |
| `ssh` | `ssh://github.com/org/repo.git` | `ssh://github.com/org/repo` | removes credentials |
| `git@` | `git@github.com:org/repo.git` | `ssh://github.com/org/repo` | canonicalized |
| `file` | `file:///tmp/myrepo` | `file:///tmp/myrepo` | local fixtures |

`ftp://` and unknown schemes are rejected. Canonical `repositoryId` is `host/path` (e.g. `github.com/org/repo`) — `https://github.com/org/repo.git`, `git@github.com:org/repo.git`, `https://user:pass@github.com/Org/Repo` all map to the same id. Local dir is `<repositories_dir>/<slug>-<hash>` (first 12 of `sha256(repositoryId)`), stable and collision-safe.

## Clone and fetch

Git CLI with `GIT_TERMINAL_PROMPT=0`; `clone` when absent, `fetch --all --prune` + fast-forward when present; never runs hooks; records `branch`, `head` (`git rev-parse HEAD`), `default_branch`; errors redact credentials and tag stage `CLONING`.

## Extraction (PyDriller)

Chronological `order="date-order"`, `only_in_branch`, optional `from_commit`; captures `hash`, author `name/email` (None if missing), `committer_date`, `message`, and per-modification `path`, `old_path`, `change_type` (ADD/MODIFY/DELETE/RENAME/COPY), `added_lines`/`deleted_lines` (None for binary/merge). Handles empty repos and shallow history.

## Filtering

POSIX normalization (`\` → `/`, `posixpath.normpath`); glob exclusion via `pathspec` + `PurePosixPath`/`fnmatch` for both separators (e.g. `**/__pycache__/**` excludes `src/__pycache__/cache.pyc`); `filteredOut` counter incremented; excluded files never reach graph.

## Change handling

| Type | Graph action |
|---|---|
| `ADD` | `File` `alive=true` |
| `MODIFY` | keep `alive=true` |
| `DELETE` | keep node, `alive=false` |
| `RENAME` confident | `File.with_rename`, single node |
| `RENAME` uncertain / `DELETE`+`ADD` | distinct nodes |
| `COPY` | distinct nodes |

`MODIFIED` edges store `changeType`, `oldPath`, `newPath`, `additions`, `deletions`.

## Labeling

Fix: case-insensitive regex list (`analysis.bug_fix_labels.patterns`) → `isFix` + `fixReason`. Revert: `Revert "..."` and `This reverts commit <hash>` → `REVERTS` only when hash resolvable. Mixed commits get both.

## Writes

Batched `UNWIND $batch AS row MERGE` via `GraphGateway` with repository-scoped keys, `batch_size` from config, parameterized Cypher, idempotent `MERGE`, repository isolation, co-change `CO_CHANGED_WITH {count, weight}` incremental (`count+1`).

## Lifecycle

`PENDING → CLONING → EXTRACTING → FILTERING → WRITING_GRAPH → COMPLETED` (or `FAILED`). Stage timestamps, counters (`commits`, `files`, `filteredOut`, `batches`), error redaction on `Repository`. Incremental via `lastIngestedHead` + `git rev-list lastHead..HEAD`; rewritten history via `git merge-base --is-ancestor` → `needsFullReingestion`; retryable from `FAILED`.

## Config

`project.repositories_dir`, `analysis.filtering.exclude`, `analysis.bug_fix_labels`, `graph.batch_size` — validated at load; invalid regex → actionable error.

## Limitations

Dynamic excludes need re-ingestion; rename threshold is conservative; force-push not auto-reconciled; no concurrent ingestion lock.
