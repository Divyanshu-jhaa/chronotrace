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

## Agent (Evidence-First LLM Layer)

The agent is the sole LLM layer, isolated over the deterministic analytics/MCP path. See [`src/chronotrace/agent/README.md`](./src/chronotrace/agent/README.md) for full details.

**Verified Meta Model API (Muse Spark) values** — pinned in `src/chronotrace/agent/model_client.py` header and `AgentConfig` validators; fail closed on mismatch. Checked 2026-11-04 against https://dev.meta.ai/docs.

| Property | Verified value | Source |
|---|---|---|
| Base URL | `https://api.meta.ai/v1` | docs/quickstart “Base URL: https://api.meta.ai/v1” |
| Chat endpoint | `POST https://api.meta.ai/v1/chat/completions` (relative `/chat/completions`) | docs/protocols/chat-completions |
| Auth header | `Authorization: Bearer <MODEL_API_KEY>` (env only, never YAML/log) | docs/authentication |
| Model identifier | `muse-spark-1.3` (pattern `^muse-spark-1\.3(-contributor)?$`) | docs/models, `chronotrace.yml` `agent.model` |
| Tool request | `tools: [{type:"function", function:{name,description,parameters}}]` | docs/tool-calling |
| Tool response | `choices[0].message.tool_calls: [{id, type:"function", function:{name,arguments}}]` | docs/tool-calling |
| Tool result message | `{role:"tool", tool_call_id:"<id>", content:"<JSON string>"}` | docs/tool-calling |
| `reasoning_effort` | top-level `reasoning_effort`; provider values `none\|minimal\|low\|medium\|high\|xhigh\|max` — ChronoTrace validates `low\|medium\|high` (default `medium`) | docs/reasoning |

Tool definitions: five MCP tools from `chronotrace.mcp_server.tools.schemas.CANONICAL_SCHEMAS`, translated via `to_provider_tools` to provider format. No new runtime dependency beyond `httpx`/`mcp` already declared.

Deterministic report path remains available without the model: `AnalyticsService.get_change_criticality(...)` and MCP `get_change_criticality` return structured `status`/`evidence`/`limitations` without `Agent`/`ModelClient`.

## Other docs

- Ingestion deep-dive: [`src/chronotrace/ingestion/README.md`](./src/chronotrace/ingestion/README.md) (transport formats, canonicalization vectors, filtering, change handling, labeling)
- Graph schema: `src/chronotrace/graph/`
- Agent layer: [`src/chronotrace/agent/README.md`](./src/chronotrace/agent/README.md) (provider contract, prompts, loop, error handling)

## Development

```bash
openspec validate --all --strict
pytest
```
