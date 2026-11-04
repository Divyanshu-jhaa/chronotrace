"""Integration tests for health and readiness — distinct contracts, mocked dependencies."""
import asyncio
from unittest.mock import MagicMock

import httpx

from chronotrace import __version__
from chronotrace.api.dependencies import AppState
from chronotrace.api.jobs import JobStore
from chronotrace.api.main import app


def _run(coro):
    return asyncio.run(coro)


class FakeGateway:
    def get_repository(self, rid):
        return None

    def get(self, rid):
        return None


def _setup_state(driver_ok=True, config_ok=True, agent_ok=True):
    store = JobStore(max_jobs=1000)
    driver = MagicMock()
    if driver_ok:
        driver.verify_connectivity = MagicMock(return_value=None)
    else:
        def _fail():
            raise Exception("neo4j down")
        driver.verify_connectivity = MagicMock(side_effect=_fail)
    gw = FakeGateway()
    agent = MagicMock() if agent_ok else None
    state = AppState(
        config=MagicMock() if config_ok else None,
        config_ok=config_ok,
        driver=driver,
        graph_gateway=gw,
        job_store=store,
        agent=agent,
    )
    # Ensure neo4j_ok reflects driver_ok for initial; health/ready will check driver.verify
    state.neo4j_ok = driver_ok
    state.agent_ok = agent_ok
    app.state.app_state = state
    import chronotrace.api.jobs as jmod
    jmod._active_store = store
    return state, store, driver, gw


def test_health_returns_ok_without_dependency_checks():
    async def _inner():
        # Even when driver is down, health should be 200 ok
        _setup_state(driver_ok=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "ok"
            assert data["version"] == __version__
            assert "uptime_seconds" in data
            assert isinstance(data["uptime_seconds"], (int, float))
            assert data["uptime_seconds"] >= 0
            assert "started_at" in data
            assert resp.headers["content-type"].startswith("application/json")
            # ensure no secret leakage
            body = resp.text.lower()
            assert "password" not in body
            assert "api_key" not in body

        # With driver up, health still ok
        _setup_state(driver_ok=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp2 = await client.get("/health")
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "ok"

    _run(_inner())


def test_ready_returns_200_when_all_pass():
    async def _inner():
        _setup_state(driver_ok=True, config_ok=True, agent_ok=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "ready"
            assert data["checks"]["neo4j"] == "ok"
            assert data["checks"]["config"] == "ok"
            assert data["checks"]["agent"] in ("ok", "degraded")

    _run(_inner())


def test_ready_returns_503_when_neo4j_unavailable():
    async def _inner():
        _setup_state(driver_ok=False, config_ok=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")
            assert resp.status_code == 503, resp.text
            data = resp.json()
            assert data["status"] == "not_ready"
            assert data["checks"]["neo4j"] == "unavailable"
            assert resp.headers["content-type"].startswith("application/json")
            # error envelope shape? ready is not error envelope but has status/checks
            # ensure 503 envelope elsewhere not required for ready, but check code handling via separate test

    _run(_inner())


def test_health_and_readiness_are_distinct_during_outage():
    async def _inner():
        _setup_state(driver_ok=False, config_ok=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            ready = await client.get("/ready")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
            assert ready.status_code == 503
            assert ready.json()["status"] == "not_ready"

    _run(_inner())


def test_ready_config_unavailable_returns_503():
    async def _inner():
        _setup_state(driver_ok=True, config_ok=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")
            assert resp.status_code == 503
            assert resp.json()["checks"]["config"] == "unavailable"

    _run(_inner())


def test_health_always_json_content_type():
    async def _inner():
        _setup_state(driver_ok=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.headers["content-type"].startswith("application/json")
            resp2 = await client.get("/ready")
            assert resp2.headers["content-type"].startswith("application/json")

    _run(_inner())


def test_openapi_lists_health_and_ready():
    async def _inner():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/openapi.json")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            paths = data.get("paths", {})
            # Health and readiness should be present
            assert "/health" in paths, f"paths: {list(paths.keys())}"
            assert "/ready" in paths, f"paths: {list(paths.keys())}"
            # Also check docs loads
            resp2 = await client.get("/docs")
            # Docs may return 200 with html
            assert resp2.status_code == 200

    _run(_inner())


def test_ready_malformed_json_and_missing_fields_envelope():
    async def _inner():
        _setup_state(driver_ok=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # malformed JSON to chat should give 422 envelope
            resp = await client.post("/chat", content=b"{bad json", headers={"Content-Type": "application/json"})
            assert resp.status_code == 422
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == "validation_error"
            assert resp.headers["content-type"].startswith("application/json")
            # missing required fields
            resp2 = await client.post("/chat", json={})
            assert resp2.status_code == 422
            assert resp2.json()["error"]["code"] == "validation_error"
            # extra unknown field
            resp3 = await client.post("/chat", json={"repository_id": "x", "question": "hi", "unknown": "x"})
            assert resp3.status_code == 422
            assert resp3.json()["error"]["code"] == "validation_error"

    _run(_inner())
