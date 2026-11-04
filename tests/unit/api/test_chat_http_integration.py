"""HTTP-level integration for chat — validates envelope via ASGI."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
from chronotrace.agent.response import AgentResponse
from chronotrace.api.dependencies import AppState
from chronotrace.api.main import app


def _run(coro):
    return asyncio.run(coro)


def test_http_chat_success_and_validation():
    async def _inner():
        mock_agent = AsyncMock()
        mock_agent.ask = AsyncMock(return_value=AgentResponse(
            answer="answer with src/foo.py",
            repository_id="test-repo",
            evidence_references=[{"path": "src/foo.py", "commit_hash": "abc", "date": "2024-01-01", "score": 0.5}],
            limitations=[],
        ))
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="test-repo"))
        mock_driver = MagicMock()
        mock_driver.verify_connectivity = MagicMock(return_value=None)
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=mock_driver)
        app.state.app_state = state
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # success
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "What is risky?"})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["answer"] != ""
            assert len(data["citations"]) > 0
            # missing repository_id -> 422
            resp2 = await client.post("/chat", json={"question": "hi"})
            assert resp2.status_code == 422
            assert resp2.json()["error"]["code"] == "validation_error"
            # empty question -> 422
            resp3 = await client.post("/chat", json={"repository_id": "test-repo", "question": ""})
            assert resp3.status_code == 422
            # unknown repo -> 404
            mock_gateway.get_repository.return_value = None
            resp4 = await client.post("/chat", json={"repository_id": "unknown", "question": "hi"})
            assert resp4.status_code == 404
            assert resp4.json()["error"]["code"] == "not_found"
            # reset
            mock_gateway.get_repository.return_value = MagicMock(id="test-repo")
            # extra unknown field -> 422
            resp5 = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi", "unknown_field": "x"})
            assert resp5.status_code == 422
            # oversized question -> 422
            resp6 = await client.post("/chat", json={"repository_id": "test-repo", "question": "x"*20000})
            assert resp6.status_code == 422
    _run(_inner())


def test_http_chat_model_failures():
    async def _inner():
        mock_gateway = MagicMock()
        mock_gateway.get_repository = MagicMock(return_value=MagicMock(id="test-repo"))
        mock_driver = MagicMock()
        mock_driver.verify_connectivity = MagicMock(return_value=None)
        mock_agent = AsyncMock()
        state = AppState(config=MagicMock(), graph_gateway=mock_gateway, agent=mock_agent, config_ok=True, driver=mock_driver)
        app.state.app_state = state
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            from chronotrace.agent.exceptions import AuthenticationError, ModelTimeoutError, MCPError
            mock_agent.ask = AsyncMock(side_effect=AuthenticationError("MODEL_API_KEY missing"))
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi"})
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "model_unavailable"
            assert "answer" not in resp.json()
            mock_agent.ask = AsyncMock(side_effect=ModelTimeoutError("timeout", timeout_seconds=5))
            resp2 = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi"})
            assert resp2.status_code == 504
            mock_agent.ask = AsyncMock(side_effect=MCPError("mcp fail"))
            resp3 = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi"})
            assert resp3.status_code == 503
            assert resp3.json()["error"]["code"] == "mcp_unavailable"
    _run(_inner())
