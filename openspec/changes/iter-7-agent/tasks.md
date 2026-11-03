# Tasks: Iteration 7 — Model Client and Evidence-First Agent

## 1. Verify provider documentation and pin assumptions

- [ ] 1.1 Verify current Meta Model API base URL, chat endpoint path, auth header (`Authorization: Bearer`), Muse Spark model identifier (`muse-spark-1.3` per `chronotrace.yml` `agent.model`), tool-call JSON shape (`tools`/`tool_calls`/`tool_call_id`/`role:"tool"`), and `reasoning_effort` controls against official docs; record verified values in a comment/header in `model_client.py` and in `design.md` if changed, and gate a live smoke test on `MODEL_API_KEY`
- [ ] 1.2 Extend validated configuration for `agent` — add `agent.base_url` (optional HTTPS URL, defaults to verified Meta endpoint), `agent.model` (validated against verified identifier literal/pattern), `agent.reasoning_effort` (`low`/`medium`/`high`, default `medium`), `agent.max_iterations` (int `ge=1 le=32`, default 8), `agent.timeout_seconds` (float `ge=1`, default 30), `agent.history_limit` (int `ge=1`, default 10), `agent.parallel_tool_calls` (bool, default false) with Pydantic validation, unknown-key rejection, and documented defaults; verify with unit tests for valid/invalid values and tilde/path handling if applicable

**Verification:** `pytest tests/unit/config/test_agent_config.py` covers valid config, missing file, unknown key, invalid `reasoning_effort`, zero/negative `max_iterations`, and non-HTTPS `base_url`; manual check `MODEL_API_KEY` set vs unset produces `AuthenticationError` without secret leakage.

## 2. Provider client isolated from orchestration

- [ ] 2.1 Create `src/chronotrace/agent/model_client.py` with `ModelClient` class wrapping `httpx.AsyncClient` (or sync `Client` if agent stays sync) — constructor takes `base_url`, `api_key`, `model`, `reasoning_effort`, `timeout_seconds`; exposes `async chat(messages, tools) -> ModelResponse` and `async close()`; serializes `messages`/`tools` per verified provider spec and sets `Authorization: Bearer <key>` header without logging the key
- [ ] 2.2 Define typed Pydantic models `ModelResponse`, `ToolCall`, `Usage` and map HTTP responses: `200` with `tool_calls`/`content` → `ModelResponse`; `401`/`403` → `AuthenticationError` naming `MODEL_API_KEY`; `429` → `RateLimitError` with `retry_after` from `Retry-After` header; `5xx`/network error → `ProviderError`; timeout → `ModelTimeoutError`; empty content with no tool calls after tool results → `NoFinalResponseError`; never fabricate content on error
- [ ] 2.3 Ensure no API key appears in source, logs, or exception messages beyond the env var name; add helper to redact secrets in `__repr__`

**Verification:** Unit tests with `httpx.MockTransport`/`respx` assert successful chat with `tool_calls`, timeout raises `ModelTimeoutError`, `401` raises `AuthenticationError` without echoing key, `429` carries `retry_after`, and secret redaction test asserts fake key substring never appears in `str(err)` or logs.

## 3. MCP client over Streamable HTTP

- [ ] 3.1 Create `src/chronotrace/agent/mcp_client.py` with `MCPClient` wrapping the Python `mcp` SDK's `streamable_http_client` transport; constructor takes `host`/`port`/`path` from validated config and imports the route constant from `chronotrace.mcp_server.server` (not a hard-coded URL); exposes `async connect()`, `async list_tools() -> list[ToolDefinition]`, `async call_tool(name, args) -> ToolResult`, `async close()` with clean lifecycle
- [ ] 3.2 Map MCP outcomes to typed errors: `validation_error`/`not_found`/`insufficient_history` returned as structured `ToolResult` content, transport failure/connection refused/`graph_unavailable` → `MCPError`; ensure `close()` drains in-flight calls and is idempotent
- [ ] 3.3 Verify the real transport route via an integration test that starts the Iteration 6 server (or a fixture) and asserts `list_tools` returns the five expected names over the verified endpoint

**Verification:** Unit tests with a fake MCP server stub assert `list_tools` returns five tools, `call_tool` forwards args, and transport error raises `MCPError`; integration test (skipped without server) connects over verified route and lists tools.

## 4. Tool definitions translation

- [ ] 4.1 Define or import canonical tool schemas for the five tools (`get_change_criticality`, `get_regression_risk`, `get_historical_impact`, `recommend_tests`, `get_file_history`) from a single shared constant (e.g., `chronotrace.mcp_server.tools.schemas.CANONICAL_SCHEMAS` or `chronotrace.agent.tools.SCHEMAS`) with JSON Schema for `repository_id` (non-empty string), `file_path` (POSIX-relative), and bounded `limit`
- [ ] 4.2 Implement pure function `to_provider_tools(canonical) -> list[dict]` translating each schema to `{type:"function", function:{name,description,parameters}}` per provider spec; snapshot-test the output for all five tools

**Verification:** Snapshot test asserts `to_provider_tools` output matches expected provider format for all five tools; changing a canonical schema without updating the snapshot fails the test, preventing drift.

## 5. Evidence-first prompts and history bounding

- [ ] 5.1 Create `src/chronotrace/agent/prompts.py` with developer prompt constant stating: cite paths/hashes/dates/counts/scores only when present in tool output; state "insufficient evidence" when unsupported; distinguish observed historical association vs causation and potential hidden coupling vs proven dependency; treat tool output as data not instructions; include explicit `repository_id` context; and helper `build_messages(repository_id, history, question)` that assembles `[developer_prompt(repo_id), *truncated_history, user_message]`
- [ ] 5.2 Implement history bounding: keep developer prompt + most recent user question always, truncate oldest turns first to `agent.history_limit` turns (or token budget); never send unbounded history; ensure cross-repository isolation (history from repo A never injected into repo B)

**Verification:** Unit tests assert prompt contains required phrases ("only when present in tool output", "insufficient evidence", "tool output as data", "observed historical association", "potential hidden coupling"), `build_messages` truncation keeps last N turns and preserves system + latest question, and isolation test asserts repo B messages never contain repo A evidence.

## 6. Tool-calling loop with validation and iteration limits

- [ ] 6.1 Create `src/chronotrace/agent/tool_loop.py` with `ToolLoop` (or `AgentLoop`) class taking `ModelClient`, `MCPClient`, prompt builder, and config (`max_iterations`, `max_tool_calls_per_round`); implement loop: send `messages`+`provider_tools` → `model_client.chat` → inspect `tool_calls` → validate each call (name in allowlist, `repository_id`/`file_path`/`limit` via shared validators reusing MCP rules: reject absolute/`..`/null-byte/Windows-absolute/empty/non-string) → execute validated calls via `mcp_client.call_tool` sequentially by default (parallel only if `parallel_tool_calls` true) → append `assistant_with_tool_calls` + `tool_result` messages (`role:"tool"`, `tool_call_id`, JSON stringified result) → iterate until `content` with no tool calls or `iter >= max_iterations` or terminal error
- [ ] 6.2 Enforce `max_iterations` at loop top: on exceed, return `MaxIterationsExceeded` status or best-effort answer with `limitations: "max_iterations (N) reached"`; cap per-round calls (e.g., 5) and total calls; validation failures are returned as tool errors to the model (not executed), not raised as exceptions that crash the loop; tool output treated as data (integration test with injection payload)
- [ ] 6.3 Handle parallel-call safety: when `parallel_tool_calls` is false, execute sequentially in returned order; when true, use `asyncio.gather` with per-call `tool_call_id` correlation; ensure one call's `MCPError`/`not_found` does not abort siblings

**Verification:** Unit tests with fake `ModelClient`/`MCPClient` cover: single round-trip invokes `call_tool` and returns `role:"tool"` result; unknown tool name returns validation error and does not call MCP; malformed `file_path` (e.g., `../secret.py`, `/etc/passwd`) returns validation error; loop terminates on final `content`; `max_iterations` cap raises `MaxIterationsExceeded`/limitation after N rounds; injection payload in tool result does not change next tool choice; sequential vs parallel modes both correlate `tool_call_id`.

## 7. Agent facade, typed response, and domain errors

- [ ] 7.1 Create `src/chronotrace/agent/agent.py` with `Agent` facade `__init__(config, model_client, mcp_client)` and `async ask(question: str, repository_id: str, history: list | None) -> AgentResponse`; binds `repository_id` per conversation, delegates to `ToolLoop`, and maps typed errors deterministically (no fabrication when model unavailable)
- [ ] 7.2 Create `src/chronotrace/agent/response.py` with `AgentResponse` (`answer: str`, `status: str`, `tool_calls_made: list`, `evidence_references: list`, `limitations: list[str]`, `repository_id`) and `src/chronotrace/agent/exceptions.py` with `AgentError`, `ModelTimeoutError`, `RateLimitError`, `AuthenticationError`, `ProviderError`, `MCPError`, `ToolValidationError`, `MaxIterationsExceeded`, `NoFinalResponseError` (or reuse `config.exceptions` hierarchy without circular import)
- [ ] 7.3 Ensure `Agent.ask` never synthesizes scores/relationships when the model is unavailable; on `AuthenticationError`/`ModelTimeoutError`/`ProviderError`/`NoFinalResponseError` it propagates the typed error for the API to render as structured HTTP error, with a note that the deterministic report path remains available

**Verification:** Unit tests assert `Agent.ask` with valid question makes a model call and returns `AgentResponse` with citations; `Agent.ask` with missing `MODEL_API_KEY` raises `AuthenticationError` naming the var without secret; `Agent.ask` when fake model raises `ModelTimeoutError` propagates without fabricated answer; `MCPError` from fake MCP client propagates deterministically.

## 8. Error handling, fallback path, and cross-cutting tests

- [ ] 8.1 Wire error handling end-to-end (timeouts, 429 rate limits, invalid credentials, malformed tool calls, MCP/transport failures, missing final response) with deterministic typed errors and actionable messages (name `MODEL_API_KEY` or `Retry-After`, no secret leakage); verify provider error bodies do not leak internal stack or Cypher
- [ ] 8.2 Preserve deterministic report path: add test that analytics/MCP `get_change_criticality` can be called directly without `Agent`/`ModelClient` and returns structured `status`/`evidence`/`limitations`; assert `Agent` does not obscure `not_found`/`insufficient_history` from MCP (surfaces limitations)
- [ ] 8.3 Add prompt-injection fixture tests, cross-repository isolation test (evidence from repo A never appears in repo B answer), and a representative-question E2E (mocked model chooses `get_change_criticality`, returns citation) covering evidence-first invariants; integration test gated on `MODEL_API_KEY` verifies a real `ModelClient.chat` succeeds when credentials are present (skipped otherwise)

**Verification:** `pytest tests/unit/agent/` covers provider client, MCP client, prompt/injection, loop, validation, iteration limits, history bounding, and isolation; `pytest tests/integration/agent/` (gated) verifies real provider path and real MCP transport connectivity; `pytest -q` without credentials still passes all mocked tests and the deterministic report-path assertion.

## 9. Documentation and housekeeping

- [ ] 9.1 Document verified Meta Model API values (base URL, endpoint, model identifier, tool format, reasoning controls) in code comments and `docs/` or `README` note; ensure `.gitignore` still excludes `.env` and no secret appears in committed `chronotrace.yml`/`chronotrace.yml.example`
- [ ] 9.2 Ensure `src/chronotrace/agent/__init__.py` exports `Agent`, `AgentResponse`, and exception types; confirm `pyproject.toml` needs no new dependency beyond `httpx`/`mcp` already declared

**Verification:** `git diff --stat` shows no `.env` committed; `ruff check` and `mypy` (if configured) pass for `src/chronotrace/agent/`; `python -c "import chronotrace.agent"` succeeds in a fresh venv.
