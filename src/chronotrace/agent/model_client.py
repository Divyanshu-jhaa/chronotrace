"""ModelClient — Meta Model API Muse Spark provider client.

Verified provider assumptions (Task 1.1 — pending live doc re-verification;
recorded here and in design.md before coding; fail closed on mismatch):

- Base URL: https://api.meta.ai/v1
  Sources: docs/quickstart "Base URL: https://api.meta.ai/v1",
           shipping blog "base_url='https://api.meta.ai/v1'",
           vectorize PR "Base URL defaults to https://api.meta.ai/v1",
           seminal.ai "API base URL https://api.meta.ai/v1".
  Default value for agent.base_url in chronotrace.yml / AgentConfig is
  exactly "https://api.meta.ai/v1". Non-HTTPS URLs are rejected by
  Pydantic validation (task 1.2).

- Chat endpoint path: POST https://api.meta.ai/v1/chat/completions
  When base_url is "https://api.meta.ai/v1", the relative path is
  "/chat/completions". Absolute path is "/v1/chat/completions".
  The API also supports POST /v1/responses (Responses API) and
  POST /v1/messages (Messages API, base host https://api.meta.ai) but
  ChronoTrace uses the OpenAI-compatible Chat Completions endpoint.
  Sources: docs/protocols/chat-completions, docs/overview, promptfoo docs
  "meta retired separate Llama API at api.llama.com 2026-07-06".

- Auth header: Authorization: Bearer <MODEL_API_KEY>
  Header name "Authorization" with "Bearer " prefix. The secret is read
  exclusively from env var MODEL_API_KEY (never YAML, never logged).
  Sources: docs/authentication, docs/quickstart "MODEL_API_KEY".

- Model identifier: muse-spark-1.3 (default)
  Verified per chronotrace.yml agent.model and docs/models.
  Variants: muse-spark-1.3-contributor also advertised (same family).
  Pattern validated: ^muse-spark-1\\.3(-contributor)?$
  Context length 1M. Non-matching identifiers are rejected by
  AgentConfig model validation (task 1.2).
  Sources: docs/models, spectrumailab guide dated 2026-10-05.

- Tool-call JSON shape (OpenAI-compatible, per docs/tool-calling):
  Request:  tools: [
    {type: "function", function: {name, description, parameters}}
  ]
  Response: choices[0].message.tool_calls: [
    {id: "<tool_call_id>", type: "function",
     function: {name, arguments: "<JSON string>"}}
  ]
  Tool result messages returned to model:
            {role: "tool", tool_call_id: "<id>", content: "<JSON string>"}
  Alternative response shape for Responses API uses content[].tool_use;
  ChronoTrace uses the chat completions shape above.
  Sources: docs/tool-calling, server tool definitions translation spec.

- reasoning_effort controls:
  Chat Completions param: reasoning_effort (top-level string enum)
  Responses API equivalent: reasoning.effort
  Accepted values per docs/reasoning: "none", "minimal", "low", "medium",
  "high", "xhigh", "max" (and "ultra" in Muse Code CLI). For Muse Spark,
  "none" is unsupported (returns 400); "max"/"xhigh" are supported by
  muse-spark-1.3. ChronoTrace config validates only low|medium|high
  (default medium) for deterministic behavior; extension to "minimal"/
  "xhigh"/"max" is intentionally rejected to keep defaults bounded.
  Sources: docs/reasoning, docs/protocols/chat-completions,
  docs/cookbook/reasoning-thinking-tokens.

If any value above diverges from live docs, update this header,
AgentConfig defaults/validators, and openspec/changes/iter-7-agent/design.md
and fail closed (raise ConfigValidationError / ProviderError).

Live smoke test is gated on MODEL_API_KEY:
  - When MODEL_API_KEY is set, an integration smoke test may POST a real
    chat request to {base_url}/chat/completions with model muse-spark-1.3
    and assert 200 + content or tool_calls.
  - When MODEL_API_KEY is unset, the test is skipped and code must raise
    AuthenticationError naming MODEL_API_KEY without echoing the secret.
  See tests/integration/agent/test_model_smoke.py (skipped without key)
  and task 2.x unit tests using httpx.MockTransport.

Security: No API key appears in source, logs, or exception messages
beyond the env var name. __repr__ helpers redact secrets.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .exceptions import (
    AuthenticationError,
    ModelTimeoutError,
    NoFinalResponseError,
    ProviderError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Verified constants — single source for tests and client
VERIFIED_BASE_URL = "https://api.meta.ai/v1"
VERIFIED_CHAT_COMPLETIONS_PATH = "/chat/completions"
VERIFIED_CHAT_COMPLETIONS_ABS_PATH = "/v1/chat/completions"
VERIFIED_AUTH_HEADER = "Authorization"
VERIFIED_MODEL = "muse-spark-1.3"
VERIFIED_MODEL_PATTERN = r"^muse-spark-1\.3(-contributor)?$"
VERIFIED_REASONING_EFFORT_VALUES = ("low", "medium", "high")
VERIFIED_PROVIDER_REASONING_VALUES = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
VERIFIED_TOOL_REQUEST_KEY = "tools"
VERIFIED_TOOL_RESPONSE_KEY = "tool_calls"
VERIFIED_TOOL_CALL_ID_KEY = "tool_call_id"
VERIFIED_TOOL_ROLE = "tool"

_REDACTED = "[REDACTED]"
_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE)


def _redact_text(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = _SK_RE.sub(_REDACTED, text)
    text = _BEARER_RE.sub(f"Bearer {_REDACTED}", text)
    return text


def _sanitize_provider_error(text: str) -> str:
    """Strip internal stack/Cypher from provider error bodies.

    Provider error bodies must not leak internal stack traces or Cypher
    queries to the caller. If the message contains stack-like tokens,
    return a generic placeholder.
    """
    low = text.lower()
    # Detect stack trace or Cypher leakage
    if any(tok in low for tok in ("traceback", "stack trace", "at line", "cypher", "match (", "return ")):
        # But allow short, non-stack messages; only strip if likely stack
        # If message is long and contains newline + stack tokens, sanitize
        if "\n" in text or "traceback" in low or "cypher" in low:
            return "Provider error (details redacted to avoid internal leakage)"
    # Also check for Cypher keywords with MATCH/RETURN on same line
    if "match" in low and "return" in low and "cypher" in low:
        return "Provider error (details redacted to avoid internal leakage)"
    return text


def _redact_key(key: str | None) -> str:
    if not key:
        return "[REDACTED: not set]"
    return _REDACTED


# ---------------------------------------------------------------------------
# Typed Pydantic models (Task 2.2)
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """Single tool call returned by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(description="Provider tool_call id")
    name: str = Field(description="Function/tool name")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Parsed JSON arguments"
    )


class Usage(BaseModel):
    """Token usage returned by the provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ModelResponse(BaseModel):
    """Typed response from ModelClient.chat."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str | None = Field(default=None, description="Assistant text")
    tool_calls: list[ToolCall] | None = Field(
        default=None, description="Tool calls requested"
    )
    usage: Usage | None = None
    model: str | None = None
    raw: dict[str, Any] | None = Field(
        default=None, description="Raw provider JSON (never contains key)"
    )


# ---------------------------------------------------------------------------
# Helpers to parse provider payload
# ---------------------------------------------------------------------------


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    if not raw or not isinstance(raw, list):
        return []
    out: list[ToolCall] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        if "function" in item and isinstance(item["function"], dict):
            func = item["function"]
            tc_id = str(item.get("id") or f"call_{idx}")
            name = str(func.get("name") or "")
            args_raw = func.get("arguments")
        else:
            tc_id = str(
                item.get("id") or item.get("tool_call_id") or f"call_{idx}"
            )
            name = str(item.get("name") or item.get("function") or "")
            args_raw = item.get("arguments")
            if args_raw is None:
                args_raw = item.get("input") or item.get("parameters")
        if not name:
            continue
        if isinstance(args_raw, str):
            s = args_raw.strip()
            if not s:
                args: dict[str, Any] = {}
            else:
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        args = {"_raw": args_raw}
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
        elif isinstance(args_raw, dict):
            args = args_raw
        elif args_raw is None:
            args = {}
        else:
            try:
                args = dict(args_raw)  # type: ignore[arg-type]
            except Exception:
                args = {"_raw": str(args_raw)}
        out.append(ToolCall(id=tc_id, name=name, arguments=args))
    return out


def _parse_usage(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    if prompt is None:
        prompt = raw.get("input_tokens") or raw.get("promptTokens")
    completion = raw.get("completion_tokens")
    if completion is None:
        completion = raw.get("output_tokens") or raw.get(
            "completionTokens"
        )
    total = raw.get("total_tokens")
    if total is None:
        total = raw.get("totalTokens")
        if total is None and prompt is not None and completion is not None:
            try:
                total = int(prompt) + int(completion)
            except Exception:
                total = None
    try:
        return Usage(
            prompt_tokens=int(prompt) if prompt is not None else None,
            completion_tokens=(
                int(completion) if completion is not None else None
            ),
            total_tokens=int(total) if total is not None else None,
        )
    except Exception:
        return None


def _endpoint_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


# ---------------------------------------------------------------------------
# ModelClient (Task 2.1)
# ---------------------------------------------------------------------------


class ModelClient:
    """Thin, stateless client wrapping httpx.AsyncClient for Muse Spark.

    Constructor takes base_url, api_key, model, reasoning_effort,
    timeout_seconds; exposes async chat(messages, tools) -> ModelResponse
    and async close(). Serializes per verified provider spec and sets
    Authorization: Bearer <key> without logging the key.

    transport / client keyword-only args are for tests (MockTransport /
    respx) and are not part of the five-arg task contract.
    """

    def __init__(
        self,
        base_url: str = VERIFIED_BASE_URL,
        api_key: str | None = None,
        model: str = VERIFIED_MODEL,
        reasoning_effort: str = "medium",
        timeout_seconds: float = 30.0,
        *,
        transport: Any | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Resolve api_key from validated config/env — caller should supply;
        # fallback via getenv only for standalone usage; env access is
        # centralized in config.loader (see single-loader contract).
        resolved_key = api_key or os.getenv("MODEL_API_KEY")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        base = base_url.strip()
        if not base.startswith("https://") and not base.startswith(
            "http://"
        ):
            raise ValueError(
                "base_url must be https:// (or http:// for tests)"
            )
        # Validate via shared validators to fail closed if assumptions stale
        from chronotrace.config.validators import (
            validate_agent_base_url,
            validate_agent_model,
            validate_reasoning_effort,
        )

        # For transport-injected tests, allow http:// mock URLs:
        # only enforce https when no test transport is present.
        if transport is None and client is None:
            validate_agent_base_url(base)
        else:
            # Still validate shape but allow http for mocks
            if not base.startswith("https://") and not base.startswith(
                "http://"
            ):
                raise ValueError("base_url must be a URL")
        validate_agent_model(model)
        validate_reasoning_effort(reasoning_effort)

        if not isinstance(resolved_key, str) or not resolved_key.strip():
            raise AuthenticationError(
                "MODEL_API_KEY is required but not set"
            )
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError(
                f"reasoning_effort must be low|medium|high, got "
                f"{reasoning_effort!r}"
            )
        try:
            tout = float(timeout_seconds)
        except Exception as exc:
            raise ValueError(
                f"timeout_seconds must be a number, got {timeout_seconds!r}"
            ) from exc
        if tout < 1:
            raise ValueError(f"timeout_seconds must be >= 1, got {tout!r}")

        self.base_url: str = base.rstrip("/")
        self._api_key: str = resolved_key
        # Keep public alias for stub compat but redact in repr
        self.api_key = self._api_key
        self.model: str = model.strip()
        self.reasoning_effort: str = reasoning_effort
        self.timeout_seconds: float = tout
        self._closed = False

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            timeout_obj = httpx.Timeout(tout)
            if transport is not None:
                self._client = httpx.AsyncClient(
                    timeout=timeout_obj, transport=transport
                )
            else:
                self._client = httpx.AsyncClient(timeout=timeout_obj)
            self._owns_client = True

    def __repr__(self) -> str:
        return (
            f"ModelClient(base_url={self.base_url!r}, model={self.model!r}, "
            f"reasoning_effort={self.reasoning_effort!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"api_key={_redact_key(self._api_key)!r})"
        )

    __str__ = __repr__

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Send messages+tools to provider and return ModelResponse.

        Maps HTTP outcomes:
        - 200 with tool_calls/content -> ModelResponse
        - 401/403 -> AuthenticationError naming MODEL_API_KEY
        - 429 -> RateLimitError with retry_after from Retry-After
        - 5xx/network -> ProviderError
        - timeout -> ModelTimeoutError
        - empty content with no tool calls -> NoFinalResponseError
        Never fabricates content on error.
        """
        if self._closed:
            raise ProviderError("ModelClient is closed")
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")

        url = _endpoint_url(self.base_url)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "reasoning_effort": self.reasoning_effort,
        }
        if tools is not None:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        logger.debug(
            "ModelClient chat model=%s reasoning=%s url=%s "
            "messages=%d tools=%d",
            self.model,
            self.reasoning_effort,
            url,
            len(messages),
            len(tools) if tools else 0,
        )

        try:
            response = await self._client.post(
                url, json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"Model request timed out after {self.timeout_seconds}s; "
                "retry with higher timeout_seconds",
                timeout_seconds=self.timeout_seconds,
            ) from exc
        except httpx.RequestError as exc:
            msg = _redact_text(str(exc))
            raise ProviderError(f"Model provider network error: {msg}") from exc
        except Exception as exc:
            if isinstance(
                exc,
                (
                    AuthenticationError,
                    RateLimitError,
                    ProviderError,
                    ModelTimeoutError,
                ),
            ):
                raise
            raise ProviderError(_redact_text(str(exc))) from exc

        status = response.status_code

        if status in (401, 403):
            raise AuthenticationError(
                f"Authentication failed (HTTP {status}); "
                "check MODEL_API_KEY is set and valid"
            )

        if status == 429:
            retry_after: int | float | None = None
            raw_retry = response.headers.get("Retry-After")
            if raw_retry is None:
                raw_retry = response.headers.get("retry-after")
            if raw_retry is not None:
                try:
                    raw_retry = raw_retry.strip()
                    if raw_retry.isdigit():
                        retry_after = int(raw_retry)
                    else:
                        try:
                            retry_after = int(float(raw_retry))
                        except ValueError:
                            retry_after = None
                except Exception:
                    retry_after = None
            raise RateLimitError(
                (
                    f"Rate limit exceeded (HTTP 429); "
                    f"retry after {retry_after}s"
                    if retry_after is not None
                    else "Rate limit exceeded (HTTP 429); "
                    "retry after Retry-After header"
                ),
                retry_after=retry_after,
            )

        if 500 <= status < 600:
            # Do not leak provider body (may contain stack/Cypher); generic status only
            raise ProviderError(f"Model provider error (status {status})")

        if 400 <= status < 500 and status not in (401, 403, 429):
            # Do not leak provider body (may contain stack/Cypher); generic status only
            raise ProviderError(f"Model provider error (status {status})")

        try:
            data: dict[str, Any] = response.json()
        except Exception as exc:
            raise ProviderError(
                f"Invalid JSON from provider: {_redact_text(str(exc))}"
            ) from exc

        if not isinstance(data, dict):
            raise ProviderError("Invalid response shape from provider")

        if "error" in data and "choices" not in data:
            err = data["error"]
            if isinstance(err, dict):
                err_msg = str(err.get("message") or err.get("code") or err)
            else:
                err_msg = str(err)
            err_msg = _redact_text(err_msg)
            err_msg = _sanitize_provider_error(err_msg)
            low = err_msg.lower()
            if (
                "api_key" in low
                or "apikey" in low
                or "authentication" in low
                or "unauthorized" in low
            ):
                raise AuthenticationError(
                    f"Authentication failed; check MODEL_API_KEY ({err_msg})"
                )
            raise ProviderError(f"Model provider error: {err_msg}")

        message: dict[str, Any] | None = None
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]  # type: ignore[assignment]
            if isinstance(first, dict):
                msg = first.get("message")  # type: ignore[assignment]
                if isinstance(msg, dict):
                    message = msg
                else:
                    message = first  # type: ignore[assignment]

        if message is None:
            if isinstance(data.get("message"), dict):
                message = data["message"]  # type: ignore[assignment]
            elif "content" in data or "tool_calls" in data:
                message = data

        if message is None:
            message = {}

        content_raw = message.get("content")
        if isinstance(content_raw, list):
            parts: list[str] = []
            for block in content_raw:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("content") or ""
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(block, str):
                    parts.append(block)
            content: str | None = "\n".join(parts) if parts else None
        elif isinstance(content_raw, str):
            content = content_raw
        elif content_raw is None:
            content = None
        else:
            content = str(content_raw)

        if content is not None and content.strip() == "":
            content = None

        tool_calls_raw = message.get("tool_calls")
        if tool_calls_raw is None:
            tool_calls_raw = message.get("toolCalls")
        tool_calls = _parse_tool_calls(tool_calls_raw)

        usage = _parse_usage(data.get("usage"))
        resp_model = data.get("model")
        if resp_model is not None:
            resp_model = str(resp_model)

        if (content is None or content == "") and not tool_calls:
            raise NoFinalResponseError(
                "Model returned empty response with no tool calls or content; "
                "no final response to surface"
            )

        return ModelResponse(
            content=content,
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
            model=resp_model,
            raw=data,
        )

    async def close(self) -> None:
        """Close underlying httpx client (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if getattr(self, "_owns_client", True):
            try:
                await self._client.aclose()
            except Exception:
                pass
        else:
            try:
                await self._client.aclose()
            except Exception:
                pass


__all__ = [
    "ModelClient",
    "ModelResponse",
    "ToolCall",
    "Usage",
    "VERIFIED_BASE_URL",
    "VERIFIED_CHAT_COMPLETIONS_PATH",
    "VERIFIED_MODEL",
    "VERIFIED_MODEL_PATTERN",
    "VERIFIED_REASONING_EFFORT_VALUES",
]
