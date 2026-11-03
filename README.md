# ChronoTrace — Evidence-first codebase risk analysis

ChronoTrace builds a repository-scoped graph of commits, files, and co-change evidence without executing repository code, powering change criticality and regression-risk analytics.

## Quickstart

```bash
pip install -e .
pytest -q
```

## Configuration

Validated `chronotrace.yml` (Pydantic v2, schema v1) — env secrets outside YAML. See [`chronotrace.yml`](./chronotrace.yml) for defaults.

## Ingestion

Ingestion turns a remote Git URL into graph evidence: acquisition → PyDriller extraction → filtering → labeling → batched Neo4j writes. Full details in [`src/chronotrace/ingestion/README.md`](./src/chronotrace/ingestion/README.md).

**Usage**

```python
from chronotrace.ingestion import IngestionService
from chronotrace.config import load_config

config = load_config("chronotrace.yml")
svc = IngestionService(config=config, gateway=graph_gateway)
result = svc.ingest("https://github.com/org/repo.git", branch="main")
print(result.state.status, result.state.counters)
```

**Config options**

| Key | Default | Purpose |
|---|---|---|
| `project.repositories_dir` | `~/.chronotrace/repositories` | Managed clone location (`<repositories_dir>/<slug>-<hash>`) |
| `analysis.filtering.exclude` | `["**/.git/**", "**/__pycache__/**", ...]` | Glob exclusion (gitwildmatch via `pathspec`) |
| `analysis.bug_fix_labels.patterns` | `["\\bfix\\b", "\\bhotfix\\b", ...]` | Case-insensitive fix regex list |
| `analysis.bug_fix_labels.case_sensitive` | `false` | Toggle case sensitivity |
| `analysis.history.max_commits` | `null` | Optional traversal cap |
| `graph.batch_size` | `500` | `UNWIND $batch AS row MERGE` batch size |

**Lifecycle states**

`PENDING → CLONING → EXTRACTING → FILTERING → WRITING_GRAPH → COMPLETED` (or `FAILED` from any state). Each stage records an ISO timestamp; counters (`commits`, `files`, `filteredOut`, `batches`) and redacted error summaries are persisted on `Repository`/`IngestionState`. Query via `svc.get_status(repository_id)`.

- Incremental: `lastIngestedHead` + `git rev-list lastHead..HEAD`, chronological, head advances only on success.
- Rewritten history: `git merge-base --is-ancestor` check → `needsFullReingestion=true` (`RewrittenHistoryError`), requires full re-ingestion.
- Retryable: `svc.retry(repository_id)` resets `FAILED` → `PENDING`.
- Observability: `INFO` logs `Writing batch 1/3 (commits)` without secrets; `filteredOut` reflects `**/__pycache__/**` etc.

**Limitations** — dynamic excludes need re-ingestion; rename threshold conservative (uncertain `DELETE+ADD` stays distinct, `COPY` distinct nodes); force-push not auto-reconciled; no concurrent ingestion lock; shallow history may be truncated.

## Other docs

- Ingestion deep-dive: [`src/chronotrace/ingestion/README.md`](./src/chronotrace/ingestion/README.md) (transport formats, canonicalization vectors, filtering, change handling, labeling)
- Graph schema: `src/chronotrace/graph/`

## Development

```bash
openspec validate --all --strict
pytest
```
