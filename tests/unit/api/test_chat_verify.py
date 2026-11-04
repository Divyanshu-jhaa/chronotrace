"""Verify chat route — thin, agent delegation, failure modes."""
import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from chronotrace.agent.response import AgentResponse
from chronotrace.api.dependencies import AppState
from chronotrace.api.routes.chat import post_chat
from chronotrace.api.schemas.chat import ChatRequest


def _run(coro):
    return asyncio.run(coro)


def make_request(state: AppState):
    mock_app = MagicMock()
    mock_app.state.app_state = state
    mock_request = MagicMock(spec=Request)
    mock_request.app = mock_app
    return mock_request


def test_success_returns_citations():
    async def _inner():
        mock_agent = AsyncMock()
        mock_agent.ask = AsyncMock(return_value=AgentResponse(
            answer="Found critical file src/foo.py with score 0.8",
            status="ok",
            tool_calls_made=[{"name": "get_change_criticality", "args": {"repository_id": "test-repo", "file_path": "src/foo.py"}}],
            evidence_references=[{"path": "src/foo.py", "commit_hash": "abc123", "date": "2024-01-01", "score": 0.8}],
            limitations=["insufficient evidence for some claims"],
            repository_id="test-repo"
        ))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="test-repo"))
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=MagicMock())
        from chronotrace.api.routes import chat as chat_mod
        orig = chat_mod.get_app_state
        chat_mod.get_app_state = lambda req: state
        try:
            body = ChatRequest(repository_id="test-repo", question="What is risky?")
            resp = await post_chat(body, make_request(state))
            assert resp.status_code == 200
            import json
            data = json.loads(resp.body.decode())
            assert data["answer"] != ""
            assert "src/foo.py" in str(data["answer"]) or len(data["citations"]) > 0
            assert len(data["citations"]) > 0
            assert len(data["evidence"]) > 0
            assert "conversation_id" in data and data["conversation_id"]
            mock_agent.ask.assert_called_once()
            args, kwargs = mock_agent.ask.call_args
            all_vals = list(args) + list(kwargs.values())
            assert "What is risky?" in all_vals or kwargs.get("question") == "What is risky?"
            assert "test-repo" in all_vals or kwargs.get("repository_id") == "test-repo"
        finally:
            chat_mod.get_app_state = orig

    _run(_inner())


def test_unknown_repo_returns_404():
    async def _inner():
        mock_agent = AsyncMock()
        mock_agent.ask = AsyncMock(return_value=AgentResponse(answer="x", repository_id="test-repo"))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=None)
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=MagicMock())
        from chronotrace.api.routes import chat as chat_mod
        orig = chat_mod.get_app_state
        chat_mod.get_app_state = lambda req: state
        try:
            body = ChatRequest(repository_id="unknown-repo", question="hi")
            resp = await post_chat(body, make_request(state))
            assert resp.status_code == 404
            import json
            data = json.loads(resp.body.decode())
            assert data["error"]["code"] == "not_found"
            mock_agent.ask.assert_not_called()
        finally:
            chat_mod.get_app_state = orig

    _run(_inner())


def test_model_unavailable_returns_503():
    async def _inner():
        mock_agent = AsyncMock()
        from chronotrace.agent.exceptions import AuthenticationError
        mock_agent.ask = AsyncMock(side_effect=AuthenticationError("MODEL_API_KEY is required"))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="test-repo"))
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=MagicMock())
        from chronotrace.api.routes import chat as chat_mod
        orig = chat_mod.get_app_state
        chat_mod.get_app_state = lambda req: state
        try:
            body = ChatRequest(repository_id="test-repo", question="hi")
            resp = await post_chat(body, make_request(state))
            assert resp.status_code == 503
            import json
            data = json.loads(resp.body.decode())
            assert data["error"]["code"] == "model_unavailable"
            assert "answer" not in data
        finally:
            chat_mod.get_app_state = orig

    _run(_inner())


def test_timeout_returns_504():
    async def _inner():
        mock_agent = AsyncMock()
        from chronotrace.agent.exceptions import ModelTimeoutError
        mock_agent.ask = AsyncMock(side_effect=ModelTimeoutError("timed out", timeout_seconds=5))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="test-repo"))
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=MagicMock())
        from chronotrace.api.routes import chat as chat_mod
        orig = chat_mod.get_app_state
        chat_mod.get_app_state = lambda req: state
        try:
            body = ChatRequest(repository_id="test-repo", question="hi")
            resp = await post_chat(body, make_request(state))
            assert resp.status_code == 504
            import json
            data = json.loads(resp.body.decode())
            assert data["error"]["code"] == "model_unavailable"
        finally:
            chat_mod.get_app_state = orig

    _run(_inner())


def test_mcp_unavailable_returns_503():
    async def _inner():
        mock_agent = AsyncMock()
        from chronotrace.agent.exceptions import MCPError
        mock_agent.ask = AsyncMock(side_effect=MCPError("mcp failure"))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="test-repo"))
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=MagicMock())
        from chronotrace.api.routes import chat as chat_mod
        orig = chat_mod.get_app_state
        chat_mod.get_app_state = lambda req: state
        try:
            body = ChatRequest(repository_id="test-repo", question="hi")
            resp = await post_chat(body, make_request(state))
            assert resp.status_code == 503
            import json
            data = json.loads(resp.body.decode())
            assert data["error"]["code"] == "mcp_unavailable"
        finally:
            chat_mod.get_app_state = orig

    _run(_inner())


def test_missing_repository_id_rejected():
    try:
        ChatRequest(question="hi")  # type: ignore
        assert False, "should have raised"
    except Exception as e:
        from pydantic import ValidationError
        assert isinstance(e, ValidationError)
        assert "repository_id" in str(e)


def test_empty_question_rejected():
    try:
        ChatRequest(repository_id="test-repo", question="   ")
        assert False
    except Exception as e:
        from pydantic import ValidationError
        assert isinstance(e, ValidationError)


def test_oversized_question_rejected():
    try:
        ChatRequest(repository_id="test-repo", question="x"*20000)
        assert False
    except Exception as e:
        from pydantic import ValidationError
        assert isinstance(e, ValidationError)


def test_thin_route_compliance():
    txt = pathlib.Path(r"D:\Meta-Global-AI-Hackathon\chronotrace\src\chronotrace\api\routes\chat.py").read_text()
    assert "MATCH" not in txt
    assert "MERGE" not in txt
    assert "session.run" not in txt
    assert "mcp_client.call_tool" not in txt
    lower = txt.lower()
    assert "system prompt" not in lower
    assert "prompt_builder" not in lower


def test_agent_receive_exact_args():
    async def _inner():
        mock_agent = AsyncMock()
        mock_agent.ask = AsyncMock(return_value=AgentResponse(answer="answer", repository_id="repo-123", evidence_references=[{"a": 1}], limitations=[]))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="repo-123"))
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=MagicMock())
        from chronotrace.api.routes import chat as chat_mod
        orig = chat_mod.get_app_state
        chat_mod.get_app_state = lambda req: state
        try:
            body = ChatRequest(repository_id="repo-123", question="exact question 42")
            resp = await post_chat(body, make_request(state))
            assert resp.status_code == 200
            assert mock_agent.ask.called
            args, kwargs = mock_agent.ask.call_args
            vals = list(args) + list(kwargs.values())
            assert "repo-123" in vals or kwargs.get("repository_id") == "repo-123"
            assert "exact question 42" in vals or kwargs.get("question") == "exact question 42"
        finally:
            chat_mod.get_app_state = orig

    _run(_inner())


def test_conversation_id_echo_and_generation():
    async def _inner():
        mock_agent = AsyncMock()
        mock_agent.ask = AsyncMock(return_value=AgentResponse(answer="answer", repository_id="test-repo", evidence_references=[], limitations=[]))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="test-repo"))
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=MagicMock())
        from chronotrace.api.routes import chat as chat_mod
        orig = chat_mod.get_app_state
        chat_mod.get_app_state = lambda req: state
        try:
            body = ChatRequest(repository_id="test-repo", question="hi", conversation_id="my-cid-123")
            resp = await post_chat(body, make_request(state))
            import json
            data = json.loads(resp.body.decode())
            assert data["conversation_id"] == "my-cid-123"
            mock_agent.ask.reset_mock()
            body2 = ChatRequest(repository_id="test-repo", question="hi")
            resp2 = await post_chat(body2, make_request(state))
            data2 = json.loads(resp2.body.decode())
            assert data2["conversation_id"] != ""
            import uuid
            uuid.UUID(data2["conversation_id"])
        finally:
            chat_mod.get_app_state = orig

    _run(_inner())
