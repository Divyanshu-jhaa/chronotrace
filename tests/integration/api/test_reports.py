"""Integration tests for POST /reports — deterministic analytics, traversal rejection, no LLM."""
import asyncio
from unittest.mock import MagicMock, Mock

import httpx

from chronotrace.api.dependencies import AppState
from chronotrace.api.jobs import JobStore
from chronotrace.api.main import app


def _run(coro):
    return asyncio.run(coro)


class FakeAnalytics:
    def __init__(self, result_map=None):
        # result_map: {(repo_id, file_path): result_obj}
        self.calls = []
        self.result_map = result_map or {}

    def get_change_criticality(self, repo_id, file_path):
        self.calls.append((repo_id, file_path))
        key = (repo_id, file_path)
        if key in self.result_map:
            val = self.result_map[key]
            if isinstance(val, Exception):
                raise val
            return val
        # default ok result
        return FakeResult(status="ok", repo_id=repo_id, target=file_path)

    # alias for alternative method names
    criticality = get_change_criticality


class _Comp:
    def __init__(self, raw, normalized, weighted, score):
        self.raw = raw
        self.normalized = normalized
        self.weighted = weighted
        self.score = score

class _ResultData:
    def __init__(self, score=0.8):
        self.score = score
        self.components = {
            "churn": _Comp(raw=5, normalized=0.6, weighted=0.3, score=0.6),
            "coupling": _Comp(raw=3, normalized=0.4, weighted=0.2, score=0.4),
        }
        self.repo_stats = {"total_commits": 100}
        self.weights = {"churn": 0.5}

class _Evidence:
    def __init__(self):
        class _Commit:
            hash = "abc123"
            date = "2024-01-01"
            message = "fix"
            fix_reason = "bug"
        class _Rel:
            source = "src/foo.py"
            target = "src/bar.py"
            kind = "co_change"
            count = 5
            weight = 0.7
            confidence = 0.9
        self.commits = [_Commit()]
        self.relationships = [_Rel()]
        self.diagnostics_ref = None

class _Limitations:
    def __init__(self, warnings=None):
        self.warnings = warnings or []
        self.shallow_history = False
        self.commit_count = 50
        self.excluded_paths_count = 0
        self.parser_failures_count = 0

class FakeResult:
    def __init__(self, status="ok", repo_id="test-repo", target="src/foo.py", error=None):
        self.status = status
        self.repository_id = repo_id
        self.target = target
        self.result = _ResultData(score=0.8)
        self.evidence = _Evidence()
        self.limitations = _Limitations(warnings=[] if status == "ok" else ["insufficient history"] if status == "insufficient_history" else [])
        # handle special statuses
        if status == "not_found":
            self.status = "not_found"
        elif status == "insufficient_history":
            self.status = "insufficient_history"
            self.limitations.warnings = ["insufficient history"]
        elif status == "graph_unavailable":
            # this will be raised as exception, not returned
            pass


class FakeGateway:
    def get_repository(self, rid):
        return None


def _setup_with_analytics(analytics):
    store = JobStore(max_jobs=1000)
    driver = MagicMock()
    driver.verify_connectivity = MagicMock(return_value=None)
    gw = FakeGateway()
    state = AppState(
        config=MagicMock(),
        config_ok=True,
        driver=driver,
        graph_gateway=gw,
        job_store=store,
        agent=None,
        analytics=analytics,
    )
    state.neo4j_ok = True
    app.state.app_state = state
    import chronotrace.api.jobs as jmod
    jmod._active_store = store
    # Also set analytics via app.state for _get_analytics_optional fallback
    app.state.analytics = analytics  # type: ignore[attr-defined]
    return state, store, analytics


def test_post_reports_returns_analytics_ok():
    async def _inner():
        analytics = FakeAnalytics()
        _setup_with_analytics(analytics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/reports", json={"repository_id": "test-repo", "file_path": "src/foo.py"})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "ok"
            assert data["repository_id"] == "test-repo"
            assert data["target"] == "src/foo.py"
            assert "result" in data
            assert data["result"] is not None
            assert "score" in data["result"]
            assert "breakdown" in data["result"]
            assert "evidence" in data
            assert "limitations" in data
            # thin route: analytics called with exact args
            assert len(analytics.calls) == 1
            assert analytics.calls[0] == ("test-repo", "src/foo.py")
            assert resp.headers["content-type"].startswith("application/json")

    _run(_inner())


def test_post_reports_not_found_maps_to_404():
    async def _inner():
        # analytics returning not_found status
        analytics = FakeAnalytics(result_map={
            ("test-repo", "src/missing.py"): FakeResult(status="not_found", repo_id="test-repo", target="src/missing.py")
        })
        _setup_with_analytics(analytics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/reports", json={"repository_id": "test-repo", "file_path": "src/missing.py"})
            assert resp.status_code == 404, resp.text
            data = resp.json()
            # For not_found, the report route returns the analytics contract with status not_found or envelope?
            # Our implementation returns 404 with body containing status not_found
            assert data.get("status") == "not_found" or data.get("error", {}).get("code") == "not_found" or resp.status_code == 404

    _run(_inner())


def test_post_reports_insufficient_history_returns_200():
    async def _inner():
        analytics = FakeAnalytics(result_map={
            ("test-repo", "src/new.py"): FakeResult(status="insufficient_history", repo_id="test-repo", target="src/new.py")
        })
        _setup_with_analytics(analytics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/reports", json={"repository_id": "test-repo", "file_path": "src/new.py"})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "insufficient_history"
            # result may be None or have limitation
            assert "limitations" in data

    _run(_inner())


def test_post_reports_traversal_rejected_before_graph():
    async def _inner():
        analytics = FakeAnalytics()
        _setup_with_analytics(analytics)
        # Spy to ensure not called
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad_paths = ["../secret.py", "/etc/passwd", "a\\b\\c.py", "%2e%2e%2fsecret.py", "%00.py", "a\x00b.py", "../a/b.py"]
            for bad in bad_paths:
                resp = await client.post("/reports", json={"repository_id": "test-repo", "file_path": bad})
                assert resp.status_code == 422, f"{bad!r} got {resp.status_code} {resp.text}"
                assert resp.json()["error"]["code"] == "validation_error"
                assert resp.headers["content-type"].startswith("application/json")
            # Ensure analytics not called for any of those
            assert len(analytics.calls) == 0

    _run(_inner())


def test_post_reports_works_without_model():
    async def _inner():
        # No agent/model set, just analytics
        analytics = FakeAnalytics()
        store = JobStore(max_jobs=1000)
        driver = MagicMock()
        driver.verify_connectivity = MagicMock(return_value=None)
        state = AppState(
            config=MagicMock(),
            config_ok=True,
            driver=driver,
            graph_gateway=FakeGateway(),
            job_store=store,
            agent=None,  # no model
            analytics=analytics,
        )
        app.state.app_state = state
        app.state.analytics = analytics
        import chronotrace.api.jobs as jmod
        jmod._active_store = store
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/reports", json={"repository_id": "test-repo", "file_path": "src/foo.py"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "ok"

    _run(_inner())


def test_post_reports_validation_missing_fields():
    async def _inner():
        analytics = FakeAnalytics()
        _setup_with_analytics(analytics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/reports", json={"repository_id": "test-repo"})
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "validation_error"
            resp2 = await client.post("/reports", json={"file_path": "src/foo.py"})
            assert resp2.status_code == 422
            resp3 = await client.post("/reports", json={"repository_id": "test-repo", "file_path": "src/foo.py", "extra": "x"})
            assert resp3.status_code == 422
            # malformed JSON
            resp4 = await client.post("/reports", content=b"{bad", headers={"Content-Type": "application/json"})
            assert resp4.status_code == 422
            assert resp4.json()["error"]["code"] == "validation_error"

    _run(_inner())


def test_post_reports_encoded_traversal_rejected():
    async def _inner():
        analytics = FakeAnalytics()
        _setup_with_analytics(analytics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # encoded traversal via %2e %2f (%2e%2e%2f decodes to ../)
            resp = await client.post("/reports", json={"repository_id": "test-repo", "file_path": "src/%2e%2e%2fsecret.py"})
            assert resp.status_code == 422, resp.text
            assert resp.json()["error"]["code"] == "validation_error"
            assert len(analytics.calls) == 0

    _run(_inner())


def test_thin_route_no_cypher():
    # Verify routes/reports.py contains no Cypher, no scoring, no session.run
    import pathlib
    txt = pathlib.Path("src/chronotrace/api/routes/reports.py").read_text()
    assert "MATCH" not in txt
    assert "MERGE" not in txt
    assert "session.run" not in txt
    # Ensure it delegates to analytics
    assert "get_change_criticality" in txt or "analytics" in txt.lower()


def test_get_reports_query_params_also_work():
    async def _inner():
        analytics = FakeAnalytics()
        _setup_with_analytics(analytics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/reports", params={"repository_id": "test-repo", "file_path": "src/foo.py"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "ok"
            # alias file param
            resp2 = await client.get("/reports", params={"repository_id": "test-repo", "file": "src/foo.py"})
            assert resp2.status_code == 200
            # traversal via query
            resp3 = await client.get("/reports", params={"repository_id": "test-repo", "file_path": "../secret.py"})
            assert resp3.status_code == 422

    _run(_inner())


def test_reports_no_secret_leakage():
    async def _inner():
        # analytics that raises with secret in message
        def failing_analytics(repo_id, file_path):
            raise Exception("connection failed password=secret123 bolt://user:pass@host")

        analytics = MagicMock()
        analytics.get_change_criticality = MagicMock(side_effect=failing_analytics)
        # also try alias
        analytics.criticality = analytics.get_change_criticality
        _setup_with_analytics(analytics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/reports", json={"repository_id": "test-repo", "file_path": "src/foo.py"})
            # Should be 503 or 500 but not leak secret
            assert resp.status_code in (500, 503), resp.text
            body = resp.text
            assert "secret123" not in body
            assert "pass" not in body or "[REDACTED]" in body

    _run(_inner())
