"""Task 8.3 — integration test gated on MODEL_API_KEY verifies real ModelClient.chat succeeds.

Skipped when MODEL_API_KEY not set (CI without credentials). When set, does a real
chat to Verified Meta Model API endpoint with model muse-spark-1.3 and asserts
200 + content or tool_calls. Never leaks secret.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

HAS_KEY = bool(os.environ.get("MODEL_API_KEY"))


@pytest.mark.skipif(not HAS_KEY, reason="MODEL_API_KEY not set — skipping live provider smoke test")
def test_model_client_live_smoke_gated_on_key():
    import asyncio

    from chronotrace.agent.model_client import ModelClient

    async def _inner():
        # Use verified endpoint from config/model_client constants
        from chronotrace.agent.model_client import VERIFIED_BASE_URL, VERIFIED_MODEL

        # If key is present, a real chat should succeed (or at least not raise auth error)
        # We use a minimal message and no tools for smoke
        client = ModelClient(
            base_url=VERIFIED_BASE_URL,
            api_key=os.environ["MODEL_API_KEY"],
            model=VERIFIED_MODEL,
            reasoning_effort="low",
            timeout_seconds=30,
        )
        try:
            # Simple smoke: ask for a short answer, no tools, expect content
            resp = await client.chat(
                messages=[
                    {"role": "system", "content": "You are a test assistant. Reply with 'ok'."},
                    {"role": "user", "content": "Say ok"},
                ],
                tools=None,
            )
            # Should return ModelResponse with content or tool_calls, not error
            assert resp is not None
            # Content should be non-empty or tool_calls present
            has_content = resp.content is not None and resp.content.strip() != ""
            has_tools = resp.tool_calls is not None and len(resp.tool_calls) > 0
            assert has_content or has_tools, f"expected content or tool_calls, got {resp}"
            # Ensure no secret leakage in raw
            raw_str = str(resp.raw) if resp.raw else ""
            assert os.environ["MODEL_API_KEY"] not in raw_str
            assert "sk-" not in raw_str or "[REDACTED]" in raw_str
        finally:
            await client.close()

    asyncio.run(_inner())


def test_model_client_smoke_skipped_without_key_shows_auth_error():
    """When MODEL_API_KEY is missing, ModelClient raises AuthenticationError naming var."""
    if HAS_KEY:
        pytest.skip("MODEL_API_KEY is set — this test verifies missing-key path")
    from chronotrace.agent.exceptions import AuthenticationError
    from chronotrace.agent.model_client import ModelClient

    try:
        ModelClient(
            base_url="https://api.meta.ai/v1",
            api_key=None,
            model="muse-spark-1.3",
            reasoning_effort="medium",
            timeout_seconds=5,
        )
        assert False, "should have raised AuthenticationError"
    except AuthenticationError as e:
        assert "MODEL_API_KEY" in str(e)
        # No secret leakage
        assert "sk-" not in str(e)
