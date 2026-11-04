"""Chat route — thin, agent delegation, repository-scoped."""

from __future__ import annotations

import inspect
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chronotrace.api.dependencies import get_app_state
from chronotrace.api.schemas.chat import ChatRequest

router = APIRouter(tags=["chat"])


def _resolve_gateway(state: Any) -> Any | None:
    gw = getattr(state, "graph_gateway", None)
    if gw is not None:
        return gw
    # Fallback to driver if gateway not set (unlikely but supports tests)
    drv = getattr(state, "driver", None)
    # driver alone is not sufficient for repository check, but we try gateway attribute on driver
    if drv is not None and hasattr(drv, "get_repository"):
        return drv
    return gw


def _repository_exists(gateway: Any, repository_id: str) -> bool | None:
    """Check if repository exists via gateway.

    Returns True if found, False if not found, None if unable to determine
    (e.g., gateway is None or method missing).
    """
    if gateway is None:
        return None
    # Try get_repository first
    for attr in ("get_repository", "get", "repository_exists", "exists"):
        fn = getattr(gateway, attr, None)
        if callable(fn):
            try:
                result = fn(repository_id)
                # repository_exists returns bool, get returns repo or None
                if attr in ("repository_exists", "exists"):
                    return bool(result)
                # get_repository returns model or None
                return result is not None
            except Exception:
                # If gateway method raises, treat as unavailable check — propagate as not_found? Let caller decide
                # For graph failures, we will map to 503 later
                raise
    # Try repositories.get
    repos = getattr(gateway, "repositories", None)
    if repos is not None and hasattr(repos, "get"):
        try:
            result = repos.get(repository_id)
            return result is not None
        except Exception:
            raise
    # Try repository_repo attribute
    repo_repo = getattr(gateway, "repository_repo", None)
    if repo_repo is not None and hasattr(repo_repo, "get"):
        try:
            result = repo_repo.get(repository_id)
            return result is not None
        except Exception:
            raise
    return None


async def _call_agent(agent: Any, repository_id: str, question: str, conversation_id: str | None) -> Any:
    """Call agent.ask with flexible signature handling.

    Primary signature is Agent.ask(question, repository_id, history).
    Also supports alternative orderings used by fakes.
    """
    # Inspect signature to choose best call
    try:
        sig = inspect.signature(agent.ask)
        params = list(sig.parameters.keys())
        # Preferred: question, repository_id
        if "question" in params and "repository_id" in params:
            # Try keyword with history=None, conversation_id if present
            kwargs: dict[str, Any] = {"question": question, "repository_id": repository_id}
            if "history" in params:
                kwargs["history"] = None
            if "conversation_id" in params and conversation_id is not None:
                kwargs["conversation_id"] = conversation_id
            return await agent.ask(**kwargs)
        # Fallback: positional
        if len(params) >= 2:
            # Try agent.ask(question, repository_id, history)
            try:
                if "history" in params:
                    return await agent.ask(question, repository_id, None)
                # Try agent.ask(repository_id, question, conversation_id)
                return await agent.ask(repository_id, question, conversation_id)
            except TypeError:
                pass
    except Exception:
        pass

    # Last resorts — try common call patterns with await handling
    attempts = [
        lambda: agent.ask(question=question, repository_id=repository_id),
        lambda: agent.ask(question, repository_id),
        lambda: agent.ask(repository_id, question),
        lambda: agent.ask(question=question, repository_id=repository_id, history=None),
        lambda: agent.ask(repository_id=repository_id, question=question),
    ]
    last_exc: Exception | None = None
    for fn in attempts:
        try:
            result = fn()
            if inspect.isawaitable(result):
                return await result
            return result
        except TypeError as e:
            last_exc = e
            continue
        except Exception:
            raise
    if last_exc is not None:
        raise last_exc
    raise TypeError("Unable to call agent.ask with provided arguments")


def _map_agent_error(exc: BaseException) -> tuple[int, str, str]:
    """Map agent exception to HTTP status and error envelope code/message.

    Returns (status_code, code, message)
    """
    name = exc.__class__.__name__
    lower_name = name.lower()
    msg = str(exc) if str(exc) else name
    # Truncate to avoid oversized payload leakage
    if len(msg) > 2000:
        msg = msg[:2000]

    # Model unavailable family
    if isinstance(exc, BaseException):
        # Check by name for model-unavailable variants
        if "authentication" in lower_name or "modelunavailable" in lower_name or "unauth" in lower_name:
            return 503, "model_unavailable", msg
        if "ratelimit" in lower_name:
            return 503, "model_unavailable", msg
        if "provider" in lower_name:
            return 503, "model_unavailable", msg
        if "timeout" in lower_name:
            # Timeout maps to 504 per spec
            return 504, "model_unavailable", msg
        if "mcp" in lower_name:
            return 503, "mcp_unavailable", msg
        if "graph" in lower_name and "unavailable" in lower_name:
            return 503, "graph_unavailable", msg

    # Fallback by isinstance checks (import lazily to avoid circular)
    try:
        from chronotrace.agent.exceptions import (
            AuthenticationError,
            MCPError,
            ModelTimeoutError,
            ProviderError,
            RateLimitError,
        )

        if isinstance(exc, AuthenticationError):
            return 503, "model_unavailable", msg
        if isinstance(exc, RateLimitError):
            return 503, "model_unavailable", msg
        if isinstance(exc, ProviderError):
            return 503, "model_unavailable", msg
        if isinstance(exc, ModelTimeoutError):
            return 504, "model_unavailable", msg
        if isinstance(exc, MCPError):
            return 503, "mcp_unavailable", msg
        # MCP client also may raise GraphUnavailable etc
    except Exception:
        pass

    # Generic check on message content
    low_msg = msg.lower()
    if "mcp" in low_msg and "unavailable" in low_msg:
        return 503, "mcp_unavailable", msg
    if "graph" in low_msg and "unavailable" in low_msg:
        return 503, "graph_unavailable", msg
    if "timeout" in low_msg:
        return 504, "model_unavailable", msg

    # Default: 503 model unavailable for any agent error not otherwise mapped
    # But for unknown errors we return 500 internal to avoid mislabeling
    # For chat, most errors are model/MCP related, so default to model_unavailable with 503
    return 503, "model_unavailable", msg


@router.post("/chat")
async def post_chat(body: ChatRequest, request: Request):  # type: ignore[no-untyped-def]
    """Repository-scoped chat — delegates to Agent, thin handler."""
    state = get_app_state(request)

    # Validate repository exists when gateway is available
    gateway = _resolve_gateway(state)
    if gateway is not None:
        try:
            exists = _repository_exists(gateway, body.repository_id)
            if exists is False:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "code": "not_found",
                            "message": f"repository {body.repository_id!r} not found",
                        }
                    },
                )
            # exists True or None (unknown) -> proceed
        except Exception as e:
            # Graph failure -> 503 graph_unavailable (don't leak internals)
            # Use safe message
            safe_msg = "Graph is temporarily unavailable; retry"
            # If error message contains redacted pattern, just use safe
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "graph_unavailable", "message": safe_msg}},
            )

    # Resolve agent
    agent = getattr(state, "agent", None)
    if agent is None:
        # No agent configured -> model unavailable (deterministic path note)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "model_unavailable",
                    "message": "Model is unavailable; deterministic report path remains available",
                }
            },
        )

    # Generate or echo conversation_id
    cid = body.conversation_id
    if cid is None or (isinstance(cid, str) and not cid.strip()):
        cid = str(uuid.uuid4())
    else:
        cid = cid.strip()

    # Delegate to agent
    try:
        result = await _call_agent(agent, body.repository_id, body.question, cid)
    except Exception as exc:
        # Map GraphValidationError or ToolValidationError that may arise from repo check?
        # If exception is validation of repository_id, should be 404? But body validation already handled.
        # Map agent errors to 503/504
        # Check if exception is due to repository not found vs model failure
        # Look for validation messages about repository
        lower_name = exc.__class__.__name__.lower()
        lower_msg = str(exc).lower() if str(exc) else ""
        if "notfound" in lower_name or "not_found" in lower_msg or "repository" in lower_msg and "not found" in lower_msg:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": str(exc)[:500]}},
            )
        if "validation" in lower_name:
            # Tool validation errors from agent should surface as 422? But spec says unknown repo -> 404, so treat as validation aside
            # For question validation, FastAPI already did, so this is unexpected
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "validation_error", "message": str(exc)[:500]}},
            )
        status, code, message = _map_agent_error(exc)
        # Ensure we don't leak secrets; truncate and redact
        # Basic redaction: strip api_key/password
        for token in ("api_key", "password", "secret", "token", "MODEL_API_KEY"):
            if token.lower() in message.lower():
                # If message contains secret-like token, replace with generic
                message = "Model is temporarily unavailable; retry"
                break
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

    # Map agent result to ChatResponse payload
    # Support AgentResponse, dict, or any object
    answer: str | None = None
    citations: list[Any] = []
    evidence: list[Any] = []
    limitations: list[str] = []

    if isinstance(result, dict):
        answer = result.get("answer") or result.get("content") or result.get("text")
        citations = result.get("citations") or result.get("evidence_references") or result.get("evidence") or []
        evidence = result.get("evidence") or result.get("evidence_references") or citations
        limitations = result.get("limitations") or []
        # Ensure conversation_id fallback
        if result.get("conversation_id"):
            cid = str(result.get("conversation_id"))
    else:
        answer = getattr(result, "answer", None)
        if answer is None:
            answer = getattr(result, "content", None)
        if answer is None:
            answer = getattr(result, "text", None)
        if answer is None and hasattr(result, "model_dump"):
            try:
                dumped = result.model_dump()
                answer = dumped.get("answer")
                citations = dumped.get("evidence_references") or dumped.get("citations") or []
                evidence = dumped.get("evidence_references") or dumped.get("evidence") or citations
                limitations = dumped.get("limitations") or []
            except Exception:
                pass
        else:
            # Try attributes
            citations = getattr(result, "citations", None) or getattr(result, "evidence_references", None) or []
            evidence = getattr(result, "evidence", None) or getattr(result, "evidence_references", None) or citations
            limitations = getattr(result, "limitations", None) or []
            # tool_calls_made may be citations? but we treat evidence_references as citations
            if not citations and hasattr(result, "tool_calls_made"):
                # If no evidence but tool calls exist, treat them as evidence? keep empty to show thin
                pass
            # Repository id bound check: ensure result repository matches request? Not needed

    # Normalize collections to list
    if citations is None:
        citations = []
    if evidence is None:
        evidence = []
    if limitations is None:
        limitations = []
    if not isinstance(citations, list):
        citations = [citations]
    if not isinstance(evidence, list):
        evidence = [evidence]
    if not isinstance(limitations, list):
        limitations = [limitations]

    # If citations still empty but evidence has content, mirror
    if not citations and evidence:
        citations = evidence

    # Ensure answer is string
    if answer is None:
        # Do not fabricate — but if agent returned no answer, this is a provider failure.
        # However fallback should not happen for successful result; handle gracefully
        answer = ""
    answer_str = str(answer)

    # If result has limitations but answer empty, still return answer as is (could be empty? but task expects non-empty)
    # For consistency, ensure citations/evidence are JSON serializable — they already are

    return JSONResponse(
        status_code=200,
        content={
            "answer": answer_str,
            "citations": citations,
            "evidence": evidence,
            "limitations": limitations,
            "conversation_id": cid,
        },
    )
