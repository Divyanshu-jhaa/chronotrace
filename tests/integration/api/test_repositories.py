"""Integration tests for repository registration, listing, status — non-blocking ingestion."""
import asyncio
import time
from unittest.mock import MagicMock

import httpx

from chronotrace.api.dependencies import AppState
from chronotrace.api.jobs import JobStore, JobStatus, JobStage
from chronotrace.api.main import app


def _run(coro):
    return asyncio.run(coro)


class FakeGateway:
    def get_repository(self, rid):
        return None

    def get(self, rid):
        return None

    def get_by_id(self, rid):
        return None


def _setup_state_with_store(store=None):
    if store is None:
        store = JobStore(max_jobs=1000)
    driver = MagicMock()
    driver.verify_connectivity = MagicMock(return_value=None)
    gw = FakeGateway()
    # Need graph_gateway for route dependency; use gw
    state = AppState(
        config=MagicMock(),
        config_ok=True,
        driver=driver,
        graph_gateway=gw,
        job_store=store,
        agent=MagicMock(),
    )
    state.neo4j_ok = True
    app.state.app_state = state
    import chronotrace.api.jobs as jmod
    jmod._active_store = store
    return state, store, gw, driver


def test_post_repositories_valid_returns_201_immediately():
    async def _inner():
        store = JobStore(max_jobs=1000)
        state, _, _, _ = _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        async def fake_run(jid, rid, url, branch=None):
            # Simulate quick transition to completed
            await store.update(jid, status=JobStatus.running, stage=JobStage.CLONING, progress=0.1)
            await asyncio.sleep(0.02)
            await store.update(jid, status=JobStatus.completed, stage=JobStage.COMPLETED, progress=1.0)

        rmod.run_ingestion = fake_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                start = time.monotonic()
                resp = await client.post("/repositories", json={"url": "https://github.com/org/repo.git"})
                elapsed_ms = (time.monotonic() - start) * 1000
                assert resp.status_code == 201, resp.text
                data = resp.json()
                assert "repository_id" in data
                assert "job_id" in data
                assert data["status"] in ("queued", "running")
                assert data["repository_id"] == "github.com/org/repo"
                assert "Location" in resp.headers
                assert "X-Job-Id" in resp.headers
                assert data["job_id"] == resp.headers["X-Job-Id"]
                assert resp.headers["Location"].startswith("/repositories/")
                # Must return within 500 ms even though ingestion mocked to take 20 ms + background
                assert elapsed_ms < 500, f"took {elapsed_ms}ms"
                # Job should be observable
                await asyncio.sleep(0.05)
                jid = data["job_id"]
                rid = data["repository_id"]
                # Poll via job_id
                resp2 = await client.get(f"/jobs/{jid}")
                assert resp2.status_code == 200, resp2.text
                assert resp2.json()["status"] in ("running", "completed")
                # Poll via repository_id
                resp3 = await client.get(f"/repositories/{rid}")
                assert resp3.status_code == 200, resp3.text
                assert resp3.json()["repository_id"] == rid
        finally:
            rmod.run_ingestion = orig

    _run(_inner())


def test_post_repositories_non_blocking_large_ingestion():
    async def _inner():
        store = JobStore(max_jobs=1000)
        state, _, _, _ = _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        async def slow_run(jid, rid, url, branch=None):
            await store.update(jid, status=JobStatus.running, stage=JobStage.CLONING)
            # Simulate 5 seconds work via to_thread sleep but we just sleep asyncio
            await asyncio.sleep(0.05)  # in test we shorten; real would be 5s via to_thread
            await store.update(jid, status=JobStatus.completed, stage=JobStage.COMPLETED)

        rmod.run_ingestion = slow_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                start = time.monotonic()
                resp = await client.post("/repositories", json={"url": "https://github.com/org/slow-repo.git"})
                elapsed = (time.monotonic() - start) * 1000
                assert resp.status_code == 201
                assert elapsed < 500, f"handler blocked for {elapsed}ms"
                # Immediately job should be queued or running
                jid = resp.json()["job_id"]
                # Don't wait for completion, check that job exists quickly
                job = await store.get(jid)
                assert job is not None
                assert job.status in (JobStatus.queued, JobStatus.running)
        finally:
            rmod.run_ingestion = orig

    _run(_inner())


def test_post_repositories_invalid_url_rejected():
    async def _inner():
        _setup_state_with_store()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for bad in ["", "not a url", "ftp://example.com/repo.git", "https://github.com/org/repo; rm -rf", "$(rm -rf)"]:
                resp = await client.post("/repositories", json={"url": bad})
                assert resp.status_code == 422, f"{bad!r} got {resp.status_code} {resp.text}"
                data = resp.json()
                assert "error" in data
                assert data["error"]["code"] == "validation_error"
                assert resp.headers["content-type"].startswith("application/json")
            # missing url
            resp2 = await client.post("/repositories", json={})
            assert resp2.status_code == 422
            # extra field
            resp3 = await client.post("/repositories", json={"url": "https://github.com/org/repo.git", "extra": "x"})
            assert resp3.status_code == 422
            # oversized? test branch too long
            resp4 = await client.post("/repositories", json={"url": "https://github.com/org/repo.git", "branch": "a"*257})
            assert resp4.status_code == 422

    _run(_inner())


def test_get_repositories_lists_two_and_pagination():
    async def _inner():
        store = JobStore(max_jobs=1000)
        _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        async def fake_run(jid, rid, url, branch=None):
            await store.update(jid, status=JobStatus.completed, stage=JobStage.COMPLETED, progress=1.0)

        rmod.run_ingestion = fake_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r1 = await client.post("/repositories", json={"url": "https://github.com/org/repo1.git"})
                assert r1.status_code == 201
                r2 = await client.post("/repositories", json={"url": "https://github.com/org/repo2.git"})
                assert r2.status_code == 201
                await asyncio.sleep(0.05)
                # list
                resp = await client.get("/repositories")
                assert resp.status_code == 200, resp.text
                data = resp.json()
                assert "repositories" in data
                assert data["total"] >= 2
                ids = [x["repository_id"] for x in data["repositories"]]
                assert "github.com/org/repo1" in ids
                assert "github.com/org/repo2" in ids
                # each entry has required fields
                for item in data["repositories"]:
                    assert "repository_id" in item
                    assert "url" in item
                    assert "ingestion_status" in item or "status" in item
                # pagination limit 1
                resp2 = await client.get("/repositories?limit=1&offset=0")
                assert resp2.status_code == 200
                assert len(resp2.json()["repositories"]) == 1
                assert resp2.json()["limit"] == 1
                assert resp2.json()["offset"] == 0
                # pagination offset 1
                resp3 = await client.get("/repositories?limit=1&offset=1")
                assert resp3.status_code == 200
                assert len(resp3.json()["repositories"]) == 1
                # invalid limit should be 422
                resp4 = await client.get("/repositories?limit=200")
                assert resp4.status_code == 422
        finally:
            rmod.run_ingestion = orig

    _run(_inner())


def test_get_repository_status_and_job_status():
    async def _inner():
        store = JobStore(max_jobs=1000)
        _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        async def fake_run(jid, rid, url, branch=None):
            await store.update(jid, status=JobStatus.running, stage=JobStage.CLONING, progress=0.1)
            await asyncio.sleep(0.01)
            await store.update(jid, stage=JobStage.EXTRACTING, progress=0.3)
            await asyncio.sleep(0.01)
            await store.update(jid, status=JobStatus.completed, stage=JobStage.COMPLETED, progress=1.0)

        rmod.run_ingestion = fake_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/repositories", json={"url": "https://github.com/org/polltest.git"})
                assert resp.status_code == 201
                rid = resp.json()["repository_id"]
                jid = resp.json()["job_id"]
                await asyncio.sleep(0.05)
                # via repository_id
                r2 = await client.get(f"/repositories/{rid}")
                assert r2.status_code == 200, r2.text
                data = r2.json()
                assert data["repository_id"] == rid
                assert data["job_id"] == jid
                assert data["status"] in ("completed", "running", "queued")
                assert "stage" in data
                assert "created_at" in data
                assert "updated_at" in data
                # via job_id
                r3 = await client.get(f"/jobs/{jid}")
                assert r3.status_code == 200
                assert r3.json()["job_id"] == jid
                # unknown returns 404 envelope
                r4 = await client.get("/repositories/does-not-exist-12345")
                assert r4.status_code == 404
                assert r4.json()["error"]["code"] == "not_found"
                r5 = await client.get("/jobs/00000000-0000-0000-0000-000000000000")
                assert r5.status_code == 404
                assert r5.json()["error"]["code"] == "not_found"
        finally:
            rmod.run_ingestion = orig

    _run(_inner())


def test_ingestion_observability_transitions():
    async def _inner():
        store = JobStore(max_jobs=1000)
        _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        # Manual progression via direct store updates to test polling sees queued->running->completed
        async def progressive_run(jid, rid, url, branch=None):
            await store.update(jid, status=JobStatus.running, stage=JobStage.CLONING, progress=0.05)
            await asyncio.sleep(0.02)
            await store.update(jid, stage=JobStage.WRITING_GRAPH, progress=0.85)
            await asyncio.sleep(0.02)
            await store.update(jid, status=JobStatus.completed, stage=JobStage.COMPLETED, progress=1.0)

        rmod.run_ingestion = progressive_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/repositories", json={"url": "https://github.com/org/observable.git"})
                assert resp.status_code == 201
                rid = resp.json()["repository_id"]
                jid = resp.json()["job_id"]
                # immediate poll may be queued, running, or already completed due to fast background execution
                r1 = await client.get(f"/repositories/{rid}")
                assert r1.status_code == 200
                assert r1.json()["status"] in ("queued", "running", "completed")
                # after 0.07s should definitely be completed (progressive_run takes ~0.04s)
                await asyncio.sleep(0.07)
                r2 = await client.get(f"/jobs/{jid}")
                assert r2.status_code == 200
                assert r2.json()["status"] == "completed"
                assert r2.json()["finished_at"] is not None
                assert r2.json()["stage"] == "COMPLETED"
                # also check updated_at advances
                r1_data = r1.json()
                r2_data = r2.json()
                # if first poll was not completed, updated_at should have advanced
                if r1_data["status"] != "completed":
                    assert r2_data["updated_at"] >= r1_data["updated_at"]
        finally:
            rmod.run_ingestion = orig

    _run(_inner())


def test_failed_ingestion_marks_failed_with_error():
    async def _inner():
        store = JobStore(max_jobs=1000)
        _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        async def failing_run(jid, rid, url, branch=None):
            await store.update(jid, status=JobStatus.running, stage=JobStage.CLONING)
            await asyncio.sleep(0.01)
            await store.update(jid, status=JobStatus.failed, stage=JobStage.FAILED, error="clone failed: network error")

        rmod.run_ingestion = failing_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/repositories", json={"url": "https://github.com/org/failtest.git"})
                assert resp.status_code == 201
                jid = resp.json()["job_id"]
                rid = resp.json()["repository_id"]
                await asyncio.sleep(0.03)
                r2 = await client.get(f"/jobs/{jid}")
                assert r2.status_code == 200
                assert r2.json()["status"] == "failed"
                assert r2.json()["error"] == "clone failed: network error"
                assert r2.json()["stage"] == "FAILED"
                assert r2.json()["finished_at"] is not None
                # Repository should show failed, not completed
                r3 = await client.get(f"/repositories/{rid}")
                assert r3.json()["status"] == "failed"
        finally:
            rmod.run_ingestion = orig

    _run(_inner())


def test_event_loop_non_blocking():
    async def _inner():
        store = JobStore(max_jobs=1000)
        _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        async def slow_run(jid, rid, url, branch=None):
            await store.update(jid, status=JobStatus.running, stage=JobStage.CLONING)
            await asyncio.sleep(0.2)  # simulate long work
            await store.update(jid, status=JobStatus.completed, stage=JobStage.COMPLETED)

        rmod.run_ingestion = slow_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/repositories", json={"url": "https://github.com/org/longrun.git"})
                assert resp.status_code == 201
                # While ingestion running, health and reports should be responsive
                start = time.monotonic()
                health = await client.get("/health")
                elapsed = (time.monotonic() - start) * 1000
                assert health.status_code == 200
                assert elapsed < 200, f"health blocked for {elapsed}ms during ingestion"
                # second request also quick
                start2 = time.monotonic()
                # reports would need analytics, but health is enough for non-blocking proof
                health2 = await client.get("/health")
                elapsed2 = (time.monotonic() - start2) * 1000
                assert elapsed2 < 200
        finally:
            rmod.run_ingestion = orig

    _run(_inner())


def test_repository_id_stable_from_url():
    async def _inner():
        store = JobStore(max_jobs=1000)
        _setup_state_with_store(store)
        import chronotrace.api.routes.repositories as rmod
        orig = rmod.run_ingestion

        async def fake_run(jid, rid, url, branch=None):
            await store.update(jid, status=JobStatus.completed, stage=JobStage.COMPLETED)

        rmod.run_ingestion = fake_run
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r1 = await client.post("/repositories", json={"url": "https://github.com/org/repo.git"})
                r2 = await client.post("/repositories", json={"url": "https://github.com/org/repo"})
                # Both should derive same repository_id (host/path normalized, scheme-insensitive)
                assert r1.json()["repository_id"] == r2.json()["repository_id"]
                assert r1.json()["repository_id"] == "github.com/org/repo"
        finally:
            rmod.run_ingestion = orig

    _run(_inner())
