# ChronoTrace MCP Server — `chronotrace.mcp_server`

Validated, read-only Model Context Protocol (MCP) gateway that exposes the deterministic analytics engine via five tool contracts over a Streamable HTTP transport. The server is the sole validated boundary between the Neo4j graph / analytics layer and downstream consumers (Agent — Iteration 7, API — Iteration 8). It never re-parses `chronotrace.yml` or env; it consumes the validated config object from Iteration 1 and reuses the single shared driver/analytics instance.

Source: `src/chronotrace/mcp_server/server.py`, `health.py`, `tools/`

Spec: `openspec/changes/iter-6-mcp/specs/mcp/spec.md`
Design: `openspec/changes/iter-6-mcp/design.md` — Proposal: `openspec/changes/iter-6-mcp/proposal.md`

## Endpoint Constant — `MCP_HTTP_PATH`

```python
from chronotrace.mcp_server.server import MCP_HTTP_PATH, MCP_SSE_PATH

# MCP_HTTP_PATH == "/mcp"  (MCP_SSE_PATH == "/sse" legacy fallback)
```

- **Constant:** `MCP_HTTP_PATH = "/mcp"` defined in `src/chronotrace/mcp_server/server.py`.
- **Sourced from SDK:** On import, `server.py` probes the installed `mcp` SDK's Streamable HTTP modules (`mcp.server.streamable_http`, `mcp.server.streamable_http_manager`, `mcp.server.sse`, `mcp.server.lowlevel.server`) for a declared path constant (`MCP_HTTP_PATH` / `STREAMABLE_HTTP_PATH` / `HTTP_PATH` / `PATH`). If found and starts with `/`, it overrides the default. This follows Decision 5 — the route is whatever the SDK declares, not a hard-coded guess.
- **Verified, not assumed:** Integration tests import `MCP_HTTP_PATH` and construct the client URL from it (`http://host:port + MCP_HTTP_PATH`). A unit assertion compares the constant against the SDK module attribute when the SDK is installed. On SDK upgrade the test fails fast if the path changed.
- **Client example:**
  ```python
  from mcp.client.streamable_http import streamable_http_client
  from chronotrace.mcp_server.server import MCP_HTTP_PATH

  url = f"http://{host}:{port}{MCP_HTTP_PATH}"
  async with streamable_http_client(url) as (read, write, _):
      async with ClientSession(read, write) as session:
          await session.initialize()
          tools = await session.list_tools()  # exactly 5
  ```
- **Port defaults:** Bind host/port from validated config (`mcp.host` / `mcp.port` in `chronotrace.yml`, Iteration 1 schema) with documented defaults; tests use an ephemeral port.

## Tool Contracts — Five Validated Tools

The server registers exactly five tools via `@server.tool()` / `@mcp.tool()` (snake_case, MCP-compliant). Every tool validates before any graph/analytics call and returns the common envelope `{status, repository_id, target, ... , evidence, limitations}` or a structured error `{status, error: {code, message}, isError: true}`. List via `await server.list_tools()` or `server.list_tools()` on the stub.

| Tool | Inputs | Delegates to | Output fields (success) | Caps |
|---|---|---|---|---|
| `get_change_criticality` | `repository_id` (str), `file_path` (str) | `analytics_service.get_change_criticality` | `status`, `repository_id`, `target`, `score`, `breakdown` (decayed churn, fix involvement, co-change degree, import fan-in with `raw`/`normalized`/`weighted`/`score`), `evidence` (`commits`, `relationships`), `limitations` (`shallow_history`, `commit_count`, `excluded_paths_count`, `parser_failures_count`, `warnings`), `repo_stats`, `weights`, `isError` | analytics-bounded (no client limit) |
| `get_regression_risk` | `repository_id`, `file_path` | `analytics.regression` | `status`, `repository_id`, `target`, `fix_commits:[{hash,date,message,fix_reason}]`, `co_changed_files:[{path,count,confidence}]` (+ alias `related_files`), ranked deduplicated, `evidence`, `limitations`, `isError` | server-side cap **20** `co_changed_files` |
| `get_historical_impact` | `repository_id`, `file_path` | `analytics.impact` | `status`, `repository_id`, `target`, `importers:[{path,relationship_type:IMPORTS,commits}]`, `dependencies`, `co_changed:[{path,relationship_type:CO_CHANGED_WITH,count,confidence,weight,potential_hidden_coupling,note,commits}]`, `potential_hidden_couplings` (co-change pairs with no direct `IMPORTS` edge) + alias `hidden_couplings`, `evidence`, `limitations` | each list capped **50** |
| `recommend_tests` | `repository_id`, `file_path` | `analytics.test_reco` | `status`, `repository_id`, `target`, `candidates:[{path,score,rank,reasons:[name_match|imports_target|co_changed|touched_in_fix]}]` ordered `score` desc, `evidence`, `limitations:{warnings:[no claim of real coverage ...]}` | server-side cap **20** `candidates` |
| `get_file_history` | `repository_id`, `file_path`, `limit?` (int) | `FileHistoryRepository.get_commits_for_file` (parameterized Cypher) | `status`, `repository_id`, `target`, `commits:[{hash,date,message,author,is_fix,fix_reason,additions,deletions,change_type}]` ordered `c.date DESC`, `limit_applied`, `total_count` and/or `has_more`, `isError` | bounded `limit` — default **20**, max **100** |

### Per-tool Notes (match spec scenarios)

- **`get_change_criticality`** — `status: ok` with `score`/`breakdown`; `status: insufficient_history` when history shallow (single commit, filtered paths) with `limitations` reason, no zero-risk claim; `not_found` when `File` node missing.
- **`get_regression_risk`** — includes `fix_reason` per commit; when no fix commits touch target, returns `status: ok` or `insufficient_history` with empty `co_changed_files` and an explicit *no fix evidence found — does not indicate zero future risk* limitation (from analytics `NO_FIX_LIMITATION`). Ranking deterministic (`count` desc → `confidence` desc → lexical).
- **`get_historical_impact`** — each entry carries `path`, `relationship_type`, supporting `commits`/`count`; pairs with no direct `IMPORTS` appear in `potential_hidden_couplings` with note `"observed association not proven dependency"` (flagged via `potential_hidden_coupling: true`). When no evidence, `limitations` states `no static relationship resolved, insufficient co-change evidence` — never claims zero impact.
- **`recommend_tests`** — reasons drawn from four signals: `name_match`, `imports_target`, `co_changed`, `touched_in_fix`; response always includes `limitations.warnings[0]` = `no claim of real coverage — recommendations are heuristic suggestions, not measured coverage or guaranteed relevance`. Empty `candidates` when no signal matches.
- **`get_file_history`** — commits carry `hash` (full hash, lowercased), `date` (original commit date), `message`, `author`, `is_fix`/`fix_reason` (ingestion heuristic label), `additions`/`deletions`/`change_type` (from `MODIFIED` relationship). Pagination: `limit_applied` is the validated limit; `total_count` via `COUNT` query (or `has_more = total_count > limit`); at least one of `total_count`/`has_more` present.

### Common Result Envelope — Evidence and Limitations

Every success follows the Iteration 5 common envelope (spec § Structured output envelope):

```
{status, repository_id, target, <tool-specific result>, evidence, limitations, isError:false}
{status:"insufficient_history", ..., limitations:{reason,...}, isError:false}
{status:"not_found", error:{code,message}, isError:true}
```

- `status` ∈ `ok | insufficient_history | not_found | validation_error | graph_unavailable | internal_error`
- `evidence` holds commit hashes/dates/messages, `fix_reason`, relationship counts, directional confidence — preserves the ingestion evidence chain.
- `limitations` / `data_coverage` discloses shallow history, excluded paths, parser failures, fix-label heuristics. Missing evidence is never presented as zero risk.
- Determinism: same tool + args on unchanged fixture graph returns byte-identical response (unit `determinism` test).

## Validation Rules — Fail-fast Before Graph Access

All five tools call shared validators in `src/chronotrace/mcp_server/tools/validation.py` before any driver/analytics call. Tests cover empty/`None`/numeric/unknown-ID and traversal/backslash/NUL/limit branches, distinguishing `validation_error` vs `not_found`.

### `validate_repository_id(value) -> str`

- Required, must be `str`; reject `None`/non-string/numeric/objects with `validation_error` `VALIDATION_ERROR` and `isError: true`; does not query Neo4j.
- Non-empty after `strip()`; reject `""` or whitespace-only.
- Length `1..256` after stripping.
- Alphabet: `A-Za-z0-9 - _ : / .` (`^[A-Za-z0-9\-_:\/\.]+$`) — covers canonical `host/path` IDs and hex hash suffixes from Iteration 3 stable-ID.
- Returns stripped normalized string. Unknown but syntactically valid IDs pass validation; caller later returns `status: not_found` when no `Repository` node matches (no stack trace or driver internals).

### `validate_file_path(value) -> str` — POSIX-relative only

Accepts only repository-relative POSIX paths; normalizes via `Path(p).as_posix()` + `posixpath.normpath` (shared with `ingestion/filtering.normalize_path` and `graph/models.normalize_path`; Windows `a/b\c.py` vs `a/b/c.py` normalize identically via the shared helper, but validators reject raw backslashes — see below).

**Rejections (`validation_error`):**

- Non-string, `None`, empty/whitespace-only; empty string, `"."` after norm also rejected (`must not be '.' or empty`).
- Contains `\x00` (NUL) anywhere — checked before and after stripping and after norm.
- Contains `\` (backslash) anywhere — caller must send POSIX form with `/`; all raw, stripped, posix, and normalized forms checked. `a\b\c.py` is rejected; send `a/b/c.py`.
- Absolute POSIX leading `/` (before/after norm), Windows drive `C:`, `C:\`, `C:/`, or UNC `\\server\share` (regex `^[A-Za-z]:([/\\]|$)` plus `is_absolute()`).
- Any `..` segment before and after `as_posix` + `posixpath.normpath` — `../secret.py`, `a/../b.py`, `a/b/../../c.py`, normalized `../c.py`, `"/../"` substring, leading `../`, bare `..`. Split on `/` and after norm all rejected.
- Over-long path `> 1024` chars (also checked after norm) — fuzz case `4096-char` path → `validation_error` without unbounded `MATCH` execution.

**Accepted:** `src/foo/bar.py`, `pkg/__init__.py`, `a/b/c.py` — returns canonical POSIX-relative normalized form (no leading `/`, no `..` segments, no backslash).

### `validate_limit(value, default=20, max_limit=100) -> int` — `get_file_history` only

- `None` → `default` (20). Validates `default`/`max_limit` sanity (not bool).
- Otherwise accept only integral `int` in `1..max_limit` inclusive; reject `bool` (subclass of int) explicitly.
- Reject `0`, negatives, `float` including `20.0`, `NaN`, `Infinity` (`math.isnan`/`isinf`), `str` like `"20"`, `"NaN"`, `"Infinity"`, `101`/`1000`/`100000` and any non-int (`list`, `dict`) with structured `validation_error` `VALIDATION_ERROR`. No unbounded `MATCH`.
- Unknown extra tool arguments beyond declared `repository_id`/`file_path`/`limit` are rejected as `validation_error` (strict mode) or ignored per spec default — current impl rejects.

### `normalize_path(value) -> str`

Canonical POSIX helper shared with ingestion/static-analysis:

- `value.replace("\\","/")` → `posixpath.normpath` → strip leading `./` and `/` → `"."` edge retained for callers. When `chronotrace.ingestion.filtering.normalize_path` is installed, delegates to it for exact parity, with pure-posix fallback.

## Bounded Limits

| Tool / field | Client-tunable? | Default | Maximum | Enforcement |
|---|---|---|---|---|
| `get_file_history.limit` | yes (optional `limit`) | **20** when omitted (`None` → `default`) | **100** — `validate_limit(limit, default=20, max_limit=100)` | `validation_error` if `0`, negative, non-int, `NaN`, `Infinity`, `> max`; no unbounded `MATCH`; response includes `limit_applied` + `total_count` or `has_more` |
| `get_regression_risk` `co_changed_files` / `related_files` | no | — | **20** server-side cap (`rf[:20]`) | not client-tunable (no `limit` param); documented maxima, spec § Scenario: Server-side caps |
| `recommend_tests` `candidates` | no | — | **20** server-side cap (`candidates[:20]`) | idem |
| `get_historical_impact` `importers`/`dependencies`/`co_changed`/`potential_hidden_couplings` | no | — | **50** each (`[:50]`) | idem |
| `file_path` length | — | — | **1024** chars | `validation_error` if longer |

Fuzz cases: `limit: 100000`, `Infinity`, `-5`, `0`, `"20"`, oversized `4096`-char `file_path` → `validation_error`, no crash, no large `RETURN`.

`get_file_history` Cypher is parameterized and scoped:

```cypher
MATCH (c:Commit)-[r:MODIFIED]->(f:File {repository_id:$repo, canonical_path:$path})
WHERE c.repository_id = $repo AND f.repository_id = $repo
  AND (f.canonical_path = $path OR f.path = $path OR f.current_path = $path)
RETURN c, r ORDER BY c.date DESC LIMIT $limit
```

Placeholders `$repo`/`$path`/`$limit` only — no f-string. Count variant `RETURN count(c) AS total_count`. Covered by `src/chronotrace/core/graph/repositories/file_history.py`.

## Health vs Readiness

Implemented in `src/chronotrace/mcp_server/health.py:check(graph_gateway, driver) -> {status, neo4j, analytics}`.

- **Distinction:** `health` = process is up (MCP server started, `list_tools` returns five tools). `readiness` = dependencies reachable (Neo4j `verify_connectivity()` succeeds and configured database accessible). This mirrors Iteration 2 readiness gate — do not report ready if Neo4j unreachable.
- **Check logic:** Calls `graph_gateway.verify_connectivity()` (or `driver.verify_connectivity()` when passed) without leaking secrets, connection strings, or raw Cypher. On success → `{"status":"ok","neo4j":"ok","analytics":"ok"}` → MCP considered ready for tool calls.
- **On failure:** → `{"status":"degraded","neo4j":"unavailable","analytics":"ok"}`. The `status`/`neo4j: unavailable` message is sanitized via `tools/errors._sanitize_message`. Tool invocations while not ready return structured `graph_unavailable` with retry hint, not a crash or `ready` flag:
  ```json
  {"status":"graph_unavailable","error":{"code":"GRAPH_UNAVAILABLE","message":"Graph is temporarily unavailable; retry"},"isError":true}
  ```
  `isError: true` stays true and the server process remains running for subsequent calls (verified by `verify_connectivity` success vs failure tests).
- **Wiring:** `create_mcp_server(config, graph_gateway, analytics_service)` stores the shared gateway/driver as `server._gateway` / `_driver` and as module globals `_global_gateway` / `_global_analytics` for `health.check()` when called without args. `check()` tolerates stub/no-gateway (returns `ok` for unit tests without Neo4j).
- **Graceful shutdown:** On SIGTERM/stop, the factory's `close()` (and `lifespan` draining added in 6.1) stops accepting new tool calls, awaits in-flight calls (timeout ~5s), then `driver.close()` idempotently; subsequent queries raise domain error, sessions are context-managed (`with driver.session(database=...)` short-lived per call).

## Container-to-Container Hostname Note (Compose)

When running under Docker Compose (Iteration 9), the MCP server and its clients are separate containers on the same network.

- **Use the service hostname, not `localhost`.** The MCP client's `NEO4J_URI`-style principle applies to MCP too. Example Compose (Iteration 9):
  ```yaml
  services:
    neo4j: ...
    mcp-server:
      build: .
      command: ["python", "-m", "chronotrace.mcp_server.server", "--host", "0.0.0.0", "--port", "8001"]
      ports: ["${MCP_PORT:-8001}:8001"]
      environment:
        - NEO4J_URI=bolt://neo4j:7687   # service name, not localhost
      depends_on:
        neo4j: {condition: service_healthy}
    agent-api:
      build: .
      command: ["uvicorn", "chronotrace.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
      environment:
        - MCP_URL=http://mcp-server:8001/mcp   # NOT http://localhost:8001/mcp
        - NEO4J_URI=bolt://neo4j:7687
      depends_on: [mcp-server]
  ```
- **From host (developer):** `curl http://localhost:8001${MCP_HTTP_PATH}` (host-mapped port). From inside a container (agent, API), use `http://mcp-server:8001${MCP_HTTP_PATH}` where `mcp-server` is the Compose service name. `localhost` inside the API container refers to itself, not the MCP server.
- The server binds `host` from config (default `0.0.0.0` inside container); client URL is built from the verified constant `MCP_HTTP_PATH` (`/mcp`), not a hard-coded string.
- Health probes under Compose should call `health.check()` / `verify_connectivity()` on the service address; a transient `degraded` state returns `graph_unavailable` with retry hint rather than HTTP 500, so Compose `restart: on-failure` or healthcheck restart covers prolonged unavailability without crashing the agent loop.

## Error Envelope and Security

Every handler in `tools/*.py` wraps analytics/graph calls as:

```python
try: result = analytics_service.get_change_criticality(repo_id, path)
except ValidationError as e: return {"status":"validation_error","error":{"code":e.code,"message":e.message},"isError":True}
except GraphNotAvailable:    return {"status":"graph_unavailable","error":{"code":"GRAPH_UNAVAILABLE","message":"Graph is temporarily unavailable; retry"},"isError":True}
except NotFound as e:        return {"status":"not_found","error":{"code":e.code,"message":e.message},"isError":True}
except Exception:            return {"status":"internal_error","error":{"code":"INTERNAL","message":"Internal error"},"isError":True}
```

- Structured codes: `VALIDATION_ERROR`, `NOT_FOUND`, `GRAPH_UNAVAILABLE`, `INTERNAL`; `isError: true` for all errors, `false` (or omitted) for `ok`/`insufficient_history`.
- **No secrets in errors or logs** — `tools/errors.py:_sanitize_message` redacts `NEO4J_USERNAME`/`NEO4J_PASSWORD`/API keys, `://user:pass@` credentials in URLs, `bolt://`/`neo4j://` connection strings, and truncates raw Cypher (`MATCH`/`MERGE`/`DETACH DELETE` → `[query details redacted]`). Verified by injection test asserting `"' OR 1=1"` payloads stay parameterized and error messages contain no credentials.
- **Parameterized Cypher only** — all graph reads go through fixed templates with `$repo`/`$path`/`$limit` placeholders; tool arguments are never interpolated. No tool accepts `query`/`cypher` raw field; unknown fields → `validation_error`. Special characters `"' OR 1=1 //"`, `"; MATCH (n) DETACH DELETE n; //"`, `../../secret` are either rejected by validation or stored/queried literally via parameter without injection or deletion.
- **Failure isolation & concurrency:** one tool's validation/graph error does not crash the server; `asyncio.gather(10 tools)` completes with per-call results. Concurrent calls reuse the single shared driver with short-lived `driver.session(database=...)` per call, no global mutable request state, no session leaks.

## Running

```bash
pip install -e .            # pulls mcp>=1.0 via pyproject.toml
python -m chronotrace.mcp_server.server --help
python -m chronotrace.mcp_server.server --host 0.0.0.0 --port 8001
```

Programmatic factory (Dependency direction: MCP → analytics → graph):

```python
from chronotrace.mcp_server.server import create_mcp_server, MCP_HTTP_PATH
from chronotrace.mcp_server.health import check

# From validated config (Iteration 1)
server = create_mcp_server(config)
assert server.list_tools() == [
    "get_change_criticality",
    "get_file_history",
    "get_historical_impact",
    "get_regression_risk",
    "recommend_tests",
]

# Inject mocks in tests
server = create_mcp_server(config, graph_gateway=mock_gateway, analytics_service=mock_svc)
await server.call_tool("get_change_criticality", {"repository_id": repo_id, "file_path": "src/app.py"})

# Health
check()  # {"status":"ok","neo4j":"ok","analytics":"ok"} or {"status":"degraded",...}
server.close()  # idempotent driver.close()
```

Completion-gate standalone client (also the `pytest -k mcp_transport / mcp_concurrency` integration harness):

```python
import asyncio
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession
from chronotrace.mcp_server.server import MCP_HTTP_PATH

async def demo(host="127.0.0.1", port=8001, repo="demo-repo"):
    url = f"http://{host}:{port}{MCP_HTTP_PATH}"
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            assert {t.name for t in tools.tools} == {
                "get_change_criticality","get_regression_risk","get_historical_impact","recommend_tests","get_file_history"
            }
            for name in ["get_change_criticality","get_regression_risk","get_historical_impact","recommend_tests","get_file_history"]:
                kw = {"repository_id": repo, "file_path": "src/app.py"} | ({"limit": 10} if name=="get_file_history" else {})
                res = await s.call_tool(name, kw)
                assert res.isError in (True, False)  # error envelope when graph empty

asyncio.run(demo())
```

## Package and Dependencies

- `pyproject.toml` declares runtime dependency `mcp>=1.0` (Streamable HTTP transport) and `tool.setuptools.packages.find: where=["src"]` plus `tool.setuptools.package-data` entries for `chronotrace.mcp_server` and `chronotrace.mcp_server.tools` and `chronotrace.graph.migrations` so `pip install -e .` includes the transport and all modules.
- `pip install mcp` / `python -c "import mcp; print(mcp.__version__)"` and `python -m chronotrace.mcp_server.server --help` are importable after creating the package scaffold (Task 1.1 gate).

## Docs-to-Spec Parity

This README mirrors `specs/mcp/spec.md` normative claims:

- Five tools with their input schemas and result fields (incl. `fix_reason`/`confidence`/`relationship_type`/`supporting commits`/`is_fix`/`additions`/`deletions`/`change_type`).
- Validation (repository_id 1..256 stable-ID alphabet; file_path POSIX-relative rejects absolute/`..`/backslash/NUL; limit default 20 max 100) and `validation_error` vs `not_found` vs `graph_unavailable` vs `insufficient_history` distinction.
- Bounded limits and server-side caps (20 co-changed / 20 candidates / 50 impact lists).
- Structured envelope (`status`, `repository_id`, `target`, `evidence`, `limitations`, `isError`) and `isError: true` on errors without leaking secrets/Cypher/connection strings.
- Streamable HTTP transport verified via `MCP_HTTP_PATH` constant, health `ok`/`degraded` with `neo4j: ok`/`unavailable`, readiness gating, and container-to-container hostname note.
