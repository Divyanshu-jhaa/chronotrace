# Agent — Evidence-First LLM Layer (Iteration 7)

The agent is the sole LLM integration, additive over the deterministic analytics/MCP path. It owns all Model API interaction and is consumed by the API `POST /chat` (Iteration 8), CLI, and UI.

## Verified Meta Model API Contract (Task 1.1, 2026-11-04)

Checked against https://dev.meta.ai/docs; pinned in `model_client.py` header, `AgentConfig` validators, and `chronotrace.yml`. Fail closed on mismatch; live smoke test gated on `MODEL_API_KEY`.

| Property | Verified value | Source |
|---|---|---|
| Base URL | `https://api.meta.ai/v1` | docs/quickstart |
| Chat endpoint | `POST /v1/chat/completions` (relative `/chat/completions` when base is `https://api.meta.ai/v1`) | docs/protocols/chat-completions |
| Auth header | `Authorization: Bearer <MODEL_API_KEY>` (env only) | docs/authentication |
| Model | `muse-spark-1.3` (accepts `muse-spark-1.3-contributor`, pattern `^muse-spark-1\.3(-contributor)?$`, 1M context) | docs/models |
| Tool request | `tools: [{type:"function", function:{name,description,parameters}}]` | docs/tool-calling |
| Tool response | `choices[0].message.tool_calls: [{id, type:"function", function:{name,arguments}}]` | docs/tool-calling |
| Tool result | `{role:"tool", tool_call_id:"<id>", content:"<JSON string>"}` | docs/tool-calling |
| reasoning_effort | Chat Completions `reasoning_effort`, Responses `reasoning.effort`; provider supports `none,minimal,low,medium,high,xhigh,max`; ChronoTrace validates `low|medium|high` (default `medium`) | docs/reasoning |

Live smoke: `tests/integration/agent/test_model_smoke.py` — skipped without `MODEL_API_KEY`, otherwise `ModelClient.chat` succeeds.

## Modules

- `model_client.py` — `httpx.AsyncClient` wrapper; `chat(messages, tools) -> ModelResponse`; error mapping (401/403→`AuthenticationError` naming `MODEL_API_KEY`, 429→`RateLimitError` with `Retry-After`, timeout→`ModelTimeoutError`, 5xx→`ProviderError`, empty→`NoFinalResponseError`); never logs key, redacts in `__repr__`; provider bodies never leak stack/Cypher.
- `mcp_client.py` — Streamable HTTP via `mcp` SDK; `connect/list_tools/call_tool/close`; imports `MCP_HTTP_PATH` from `chronotrace.mcp_server.server`; `validation_error/not_found/insufficient_history` as `ToolResult` content, `graph_unavailable`/transport→`MCPError`; idempotent `close`.
- `tools.py` — re-exports `CANONICAL_SCHEMAS` from `mcp_server.tools.schemas` and `to_provider_tools` pure translation.
- `prompts.py` — `DEVELOPER_PROMPT` with evidence-first invariants (cite only when present in tool output, insufficient evidence, observed historical association vs causation, potential hidden coupling vs proven dependency, tool output as data, repository_id context) and `build_messages` with bounded, isolated history.
- `tool_loop.py` — bounded `ToolLoop` (validate → execute sequential/parallel with `tool_call_id` correlation → `role:"tool"` messages; `max_iterations` and per-round/total caps; validation failures returned as tool errors).
- `agent.py` — `Agent` facade `ask(question, repository_id, history) -> AgentResponse`; binds `repository_id`, delegates to `ToolLoop`, propagates typed errors deterministically with note that deterministic report path remains available.
- `response.py` — `AgentResponse` (`answer`, `status`, `tool_calls_made`, `evidence_references`, `limitations`, `repository_id`).
- `exceptions.py` — `AgentError`, `AuthenticationError`, `RateLimitError`, `ProviderError`, `ModelTimeoutError`, `NoFinalResponseError`, `MCPError`, `ToolValidationError`, `MaxIterationsExceeded` (secret redaction in `__str__`/`__repr__`).

## Deterministic Report Path

`AnalyticsService.get_change_criticality(repo, path)` and MCP `get_change_criticality` work without `Agent`/`ModelClient`, returning structured `status`/`evidence`/`limitations`. `Agent` surfaces `not_found`/`insufficient_history` as `limitations`, never obscures.

## Error Handling (Task 8.1)

Deterministic typed errors with actionable messages (name `MODEL_API_KEY` or `Retry-After`, no secret leakage). Provider error bodies never leak stack or Cypher (generic status only, sanitized). See `tests/unit/agent/test_error_handling.py`.

## Security

- Secrets from `MODEL_API_KEY` env only; never YAML, never logged; `__repr__` redacts.
- `.gitignore` excludes `.env`; `chronotrace.yml`/`chronotrace.yml.example` contain no secrets.
- No new runtime dependency beyond `httpx`/`mcp` already declared in `pyproject.toml`.

## History & Isolation

`build_messages` keeps developer prompt + most recent question, truncates oldest to `history_limit` (default 10), filters by `repository_id` metadata, never sends unbounded history. Cross-repo isolation tested in `test_cross_repo_isolation.py`.
