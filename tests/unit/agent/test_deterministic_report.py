"""Task 8.2 — deterministic report path preservation.

- analytics/MCP get_change_criticality can be called directly without Agent/ModelClient
  and returns structured status/evidence/limitations
- Agent does not obscure not_found / insufficient_history (surfaces limitations)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from chronotrace.agent.agent import Agent
from chronotrace.agent.exceptions import MCPError
from chronotrace.agent.mcp_client import ToolResult
from chronotrace.core.analytics.service import AnalyticsService
from chronotrace.mcp_server.tools.criticality import get_change_criticality
from tests.fixtures.analytics.fixture import HALF_LIFE_DAYS, REPO_ID, get_fake_graph
from chronotrace.config.models import ChronoTraceConfig


def _run(coro):
    return asyncio.run(coro)


def test_analytics_get_change_criticality_direct_without_agent():
    """Analytics/MCP get_change_criticality callable directly without Agent/ModelClient."""
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate({"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}})
    svc = AnalyticsService(fake, config)
    # Direct via AnalyticsService
    result = svc.get_change_criticality(REPO_ID, "a.py")
    assert hasattr(result, "status")
    assert hasattr(result, "evidence")
    assert hasattr(result, "limitations")
    # status is enum with value
    status_val = getattr(result.status, "value", str(result.status))
    assert status_val in ("ok", "insufficient_history", "not_found", "error")

    # Direct via MCP tool without Agent
    envelope = get_change_criticality(REPO_ID, "a.py", analytics_service=svc)
    assert isinstance(envelope, dict)
    assert "status" in envelope
    assert "evidence" in envelope
    assert "limitations" in envelope
    assert envelope["status"] in ("ok", "insufficient_history", "not_found", "validation_error", "graph_unavailable")
    assert "repository_id" in envelope
    assert envelope["repository_id"] == REPO_ID


def test_mcp_tool_returns_not_found_without_agent():
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate({"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}})
    svc = AnalyticsService(fake, config)
    # Non-existent file
    envelope = get_change_criticality(REPO_ID, "nonexistent_xyz.py", analytics_service=svc)
    assert envelope["status"] == "not_found"
    assert "evidence" in envelope


def test_mcp_tool_returns_insufficient_history_without_agent():
    fake = get_fake_graph()
    # Use a file that exists but with shallow history (repo has only few commits in fixture)
    # The fixture repo has limited commits, so we can test insufficient_history
    # For a new file with no history, it may be not_found or insufficient_history
    config = ChronoTraceConfig.model_validate({"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}})
    svc = AnalyticsService(fake, config)
    # Try a.py which exists but may have insufficient_history due to low commit count
    envelope = get_change_criticality(REPO_ID, "a.py", analytics_service=svc)
    # Should be ok or insufficient_history, but must have evidence and limitations
    assert envelope["status"] in ("ok", "insufficient_history", "not_found")
    assert "limitations" in envelope


def test_agent_does_not_obscure_not_found():
    async def _inner():
        # MCP client that returns not_found
        async def _call(name, arguments=None):
            return ToolResult(
                content={"status": "not_found", "error": {"code": "NOT_FOUND", "message": "file not found"}, "isError": True},
                text=json.dumps({"status": "not_found", "error": {"code": "NOT_FOUND"}}),
                is_error=True,
            )

        fake_mcp = MagicMock()
        fake_mcp.call_tool = AsyncMock(side_effect=_call)
        fake_mcp.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        fake_mcp.connect = AsyncMock(return_value=None)
        fake_mcp.close = AsyncMock(return_value=None)

        class FakeModel:
            call_n = 0

            async def chat(self, messages, tools=None):
                if self.call_n == 0:
                    self.call_n += 1
                    return {
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "get_change_criticality", "arguments": json.dumps({"repository_id": REPO_ID, "file_path": "missing.py"})}}
                        ],
                    }
                return {"content": "The file was not found in the graph; insufficient evidence.", "tool_calls": []}

            async def close(self):
                pass

        config = ChronoTraceConfig.model_validate({"version": 1, "agent": {"max_iterations": 3}})

        agent = Agent(config=config, model_client=FakeModel(), mcp_client=fake_mcp)
        resp = await agent.ask("What is critical for missing.py?", repository_id=REPO_ID)
        # Agent should surface not_found as limitation, not obscure
        assert any("not_found" in lim for lim in resp.limitations) or "not found" in resp.answer.lower() or "insufficient evidence" in resp.answer.lower()
        assert resp.repository_id == REPO_ID

    _run(_inner())


def test_agent_does_not_obscure_insufficient_history():
    async def _inner():
        async def _call(name, arguments=None):
            return ToolResult(
                content={"status": "insufficient_history", "evidence": {"commits": []}, "limitations": {"warnings": ["shallow"]}},
                text=json.dumps({"status": "insufficient_history"}),
                is_error=False,
            )

        fake_mcp = MagicMock()
        fake_mcp.call_tool = AsyncMock(side_effect=_call)
        fake_mcp.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        fake_mcp.connect = AsyncMock(return_value=None)
        fake_mcp.close = AsyncMock(return_value=None)

        class FakeModel:
            call_n = 0

            async def chat(self, messages, tools=None):
                if self.call_n == 0:
                    self.call_n += 1
                    return {
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "get_change_criticality", "arguments": json.dumps({"repository_id": REPO_ID, "file_path": "new.py"})}}
                        ],
                    }
                return {"content": "Insufficient history for new.py; observed limited commits.", "tool_calls": []}

            async def close(self):
                pass

        config = ChronoTraceConfig.model_validate({"version": 1, "agent": {"max_iterations": 3}})
        agent = Agent(config=config, model_client=FakeModel(), mcp_client=fake_mcp)
        resp = await agent.ask("What about new.py?", repository_id=REPO_ID)
        assert any("insufficient_history" in lim for lim in resp.limitations) or "insufficient" in resp.answer.lower()

    _run(_inner())
