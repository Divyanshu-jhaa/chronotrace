"""Integration tests for POST /chat — repository-scoped agent delegation."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx

from chronotrace.agent.response import AgentResponse
from chronotrace.api.dependencies import AppState
from chronotrace.api.jobs import JobStore
from chronotrace.api.main import app


def _run(coro):
    return asyncio.run(coro)


class FakeGateway:
    def __init__(self, exists=True):
        self._exists = exists

    def get_repository(self, rid):
        if self._exists:
            # Return a mock with id
            m = MagicMock()
            m.repository_id = rid
            m.id = rid
            return m
        return None

    def get(self, rid):
        return self.get_repository(rid)


def _setup_state_with_agent(agent, gateway_exists=True):
    store = JobStore(max_jobs=1000)
    driver = MagicMock()
    driver.verify_connectivity = MagicMock(return_value=None)
    gw = FakeGateway(exists=gateway_exists)
    state = AppState(
        config=MagicMock(),
        config_ok=True,
        driver=driver,
        graph_gateway=gw,
        job_store=store,
        agent=agent,
    )
    app.state.app_state = state
    import chronotrace.api.jobs as jmod
    jmod._active_store = store
    return state, store, gw


def test_chat_success_returns_evidence_grounded_answer():
    async def _inner():
        agent = AsyncMock()
        agent.ask = AsyncMock(return_value=AgentResponse(
            answer="The file src/foo.py has high churn",
            repository_id="test-repo",
            evidence_references=[{"path": "src/foo.py", "commit_hash": "abc123", "date": "2024-01-01", "score": 0.8}],
            limitations=["limited history"],
            tool_calls_made=[{"name": "get_change_criticality", "args": {}}],
        ))
        _setup_state_with_agent(agent, gateway_exists=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "What is risky?"})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert "answer" in data
            assert data["answer"] != ""
            assert "citations" in data
            assert len(data["citations"]) > 0
            assert "evidence" in data
            assert "limitations" in data
            assert "conversation_id" in data
            assert data["conversation_id"] != ""
            # ensure agent called with correct args
            assert agent.ask.called
            args, kwargs = agent.ask.call_args
            vals = list(args) + list(kwargs.values())
            assert "test-repo" in vals or kwargs.get("repository_id") == "test-repo"
            assert "What is risky?" in vals or kwargs.get("question") == "What is risky?"
            assert resp.headers["content-type"].startswith("application/json")

    _run(_inner())


def test_chat_missing_repository_id_rejected():
    async def _inner():
        agent = AsyncMock()
        agent.ask = AsyncMock(return_value=AgentResponse(answer="x", repository_id="test-repo"))
        _setup_state_with_agent(agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"question": "hi"})
            assert resp.status_code == 422, resp.text
            assert resp.json()["error"]["code"] == "validation_error"
            # empty string
            resp2 = await client.post("/chat", json={"repository_id": "", "question": "hi"})
            assert resp2.status_code == 422
            # whitespace
            resp3 = await client.post("/chat", json={"repository_id": "   ", "question": "hi"})
            assert resp3.status_code == 422
            assert not agent.ask.called

    _run(_inner())


def test_chat_unknown_repository_returns_404():
    async def _inner():
        agent = AsyncMock()
        agent.ask = AsyncMock(return_value=AgentResponse(answer="x", repository_id="unknown"))
        _setup_state_with_agent(agent, gateway_exists=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "unknown", "question": "hi"})
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "not_found"
            assert not agent.ask.called

    _run(_inner())


def test_chat_model_unavailable_returns_503_not_fabricated():
    async def _inner():
        from chronotrace.agent.exceptions import AuthenticationError, RateLimitError, ProviderError

        for exc, expected_code in [
            (AuthenticationError("MODEL_API_KEY is required"), "model_unavailable"),
            (RateLimitError("rate limited", retry_after=10), "model_unavailable"),
            (ProviderError("provider down"), "model_unavailable"),
        ]:
            agent = AsyncMock()
            agent.ask = AsyncMock(side_effect=exc)
            _setup_state_with_agent(agent)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi"})
                assert resp.status_code == 503, f"{exc} got {resp.status_code} {resp.text}"
                data = resp.json()
                assert data["error"]["code"] == expected_code
                assert "answer" not in data
                # ensure no secret leakage if message contains secret? not needed here

        # timeout -> 504
        from chronotrace.agent.exceptions import ModelTimeoutError

        agent2 = AsyncMock()
        agent2.ask = AsyncMock(side_effect=ModelTimeoutError("timed out", timeout_seconds=5))
        _setup_state_with_agent(agent2)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi"})
            assert resp.status_code == 504, resp.text
            assert resp.json()["error"]["code"] == "model_unavailable"

    _run(_inner())


def test_chat_mcp_unavailable_returns_503():
    async def _inner():
        from chronotrace.agent.exceptions import MCPError

        agent = AsyncMock()
        agent.ask = AsyncMock(side_effect=MCPError("mcp failure"))
        _setup_state_with_agent(agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi"})
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "mcp_unavailable"

    _run(_inner())


def test_chat_extra_and_oversized_rejected():
    async def _inner():
        agent = AsyncMock()
        agent.ask = AsyncMock(return_value=AgentResponse(answer="x", repository_id="test-repo"))
        _setup_state_with_agent(agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi", "unknown_field": "x"})
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "validation_error"
            resp2 = await client.post("/chat", json={"repository_id": "test-repo", "question": "x" * 20000})
            assert resp2.status_code == 422
            # malformed JSON
            resp3 = await client.post("/chat", content=b"{bad", headers={"Content-Type": "application/json"})
            assert resp3.status_code == 422
            assert resp3.json()["error"]["code"] == "validation_error"

    _run(_inner())


def test_chat_empty_question_rejected():
    async def _inner():
        agent = AsyncMock()
        agent.ask = AsyncMock(return_value=AgentResponse(answer="x", repository_id="test-repo"))
        _setup_state_with_agent(agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": ""})
            assert resp.status_code == 422
            resp2 = await client.post("/chat", json={"repository_id": "test-repo", "question": "   "})
            assert resp2.status_code == 422

    _run(_inner())


def test_chat_thin_route_and_correct_delegation():
    # Verify handler is thin and delegates correctly
    import pathlib

    txt = pathlib.Path("src/chronotrace/api/routes/chat.py").read_text()
    assert "MATCH" not in txt
    assert "MERGE" not in txt
    assert "session.run" not in txt
    assert "mcp_client.call_tool" not in txt

    async def _inner():
        agent = AsyncMock()
        agent.ask = AsyncMock(return_value=AgentResponse(answer="answer", repository_id="repo-123", evidence_references=[{"path": "src/foo.py"}], limitations=[]))
        _setup_state_with_agent(agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "repo-123", "question": "exact question 42"})
            assert resp.status_code == 200
            assert agent.ask.called
            args, kwargs = agent.ask.call_args
            vals = list(args) + list(kwargs.values())
            assert "repo-123" in vals or kwargs.get("repository_id") == "repo-123"
            assert "exact question 42" in vals or kwargs.get("question") == "exact question 42"

    _run(_inner())


def test_chat_conversation_id_echo_and_generation():
    async def _inner():
        agent = AsyncMock()
        agent.ask = AsyncMock(return_value=AgentResponse(answer="answer", repository_id="test-repo", evidence_references=[], limitations=[]))
        _setup_state_with_agent(agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi", "conversation_id": "my-cid-123"})
            assert resp.status_code == 200
            assert resp.json()["conversation_id"] == "my-cid-123"
            resp2 = await client.post("/chat", json={"repository_id": "test-repo", "question": "hi"})
            assert resp2.status_code == 200
            cid2 = resp2.json()["conversation_id"]
            assert cid2 != ""
            import uuid

            uuid.UUID(cid2)

    _run(_inner())
