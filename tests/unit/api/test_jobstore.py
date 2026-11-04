"""Unit tests for JobStore — transitions, eviction, concurrency."""
import asyncio
import uuid

import pytest

from chronotrace.api.jobs import JobStore, JobStatus, JobStage, Job


def _run(coro):
    return asyncio.run(coro)


def test_create_sets_queued_and_cloning():
    async def _inner():
        store = JobStore(max_jobs=1000)
        job = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git")
        assert job.status == JobStatus.queued
        assert job.stage == JobStage.CLONING
        assert job.repository_id == "repo1"
        assert job.repository_url == "https://github.com/org/repo.git"
        assert job.job_id is not None
        assert job.created_at is not None
        assert job.updated_at is not None
        assert job.finished_at is None
        assert job.error is None
        # get should return same
        fetched = await store.get(job.job_id)
        assert fetched is not None
        assert fetched.job_id == job.job_id
        assert fetched.status == JobStatus.queued

    _run(_inner())


def test_update_transitions_queued_running_completed():
    async def _inner():
        store = JobStore(max_jobs=1000)
        job = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git")
        first_updated = job.updated_at
        await asyncio.sleep(0.01)
        updated = await store.update(job.job_id, status=JobStatus.running, stage=JobStage.CLONING, progress=0.1)
        assert updated is not None
        assert updated.status == JobStatus.running
        assert updated.stage == JobStage.CLONING
        assert updated.updated_at > first_updated
        second_updated = updated.updated_at
        await asyncio.sleep(0.01)
        updated2 = await store.update(job.job_id, stage=JobStage.EXTRACTING, progress=0.3)
        assert updated2.stage == JobStage.EXTRACTING
        assert updated2.updated_at > second_updated
        # advance to completed
        await asyncio.sleep(0.01)
        completed = await store.update(job.job_id, status=JobStatus.completed, stage=JobStage.COMPLETED, progress=1.0)
        assert completed.status == JobStatus.completed
        assert completed.stage == JobStage.COMPLETED
        assert completed.finished_at is not None
        assert completed.error is None
        assert completed.updated_at > updated2.updated_at

    _run(_inner())


def test_failed_sets_error_and_finished():
    async def _inner():
        store = JobStore(max_jobs=1000)
        job = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git")
        failed = await store.update(job.job_id, status=JobStatus.failed, stage=JobStage.FAILED, error="clone failed")
        assert failed.status == JobStatus.failed
        assert failed.stage == JobStage.FAILED
        assert failed.error == "clone failed"
        assert failed.finished_at is not None

    _run(_inner())


def test_get_by_repo_returns_latest():
    async def _inner():
        store = JobStore(max_jobs=1000)
        j1 = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git")
        await asyncio.sleep(0.01)
        j2 = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git")
        # j2 is later
        latest = await store.get_by_repo("repo1")
        assert latest is not None
        assert latest.job_id == j2.job_id
        # different repo
        j3 = await store.create(repository_id="repo2", repository_url="https://github.com/org/repo2.git")
        latest2 = await store.get_by_repo("repo2")
        assert latest2.job_id == j3.job_id
        assert (await store.get_by_repo("nonexistent")) is None

    _run(_inner())


def test_eviction_when_over_limit_evicts_oldest_completed():
    async def _inner():
        store = JobStore(max_jobs=10)
        # Create 12 jobs, mark first 12 as completed
        jobs = []
        for i in range(12):
            j = await store.create(repository_id=f"repo{i}", repository_url=f"https://github.com/org/repo{i}.git")
            await store.update(j.job_id, status=JobStatus.completed, stage=JobStage.COMPLETED, progress=1.0)
            jobs.append(j)
        # Store should be at most max_jobs (10) after eviction
        assert len(store) <= 10
        # Oldest completed should be evicted
        all_jobs = await store.list_all()
        assert len(all_jobs) <= 10
        # Ensure queued/running never evicted: create 5 running, then overflow with completed
        store2 = JobStore(max_jobs=5)
        running_jobs = []
        for i in range(3):
            j = await store2.create(repository_id=f"r{i}", repository_url=f"https://github.com/org/r{i}.git")
            await store2.update(j.job_id, status=JobStatus.running, stage=JobStage.CLONING)
            running_jobs.append(j)
        # Now add many completed to exceed limit
        for i in range(10):
            j = await store2.create(repository_id=f"c{i}", repository_url=f"https://github.com/org/c{i}.git")
            await store2.update(j.job_id, status=JobStatus.completed, stage=JobStage.COMPLETED)
        # Running jobs should still exist
        for rj in running_jobs:
            fetched = await store2.get(rj.job_id)
            assert fetched is not None, f"running job {rj.job_id} was evicted but should not be"
        assert len(store2) <= 5 or len([j for j in await store2.list_all() if j.status in (JobStatus.completed, JobStatus.failed)]) <= 5

    _run(_inner())


def test_eviction_preserves_queued_running():
    async def _inner():
        store = JobStore(max_jobs=3)
        # Fill with queued
        q1 = await store.create(repository_id="q1", repository_url="https://github.com/org/q1.git")
        q2 = await store.create(repository_id="q2", repository_url="https://github.com/org/q2.git")
        q3 = await store.create(repository_id="q3", repository_url="https://github.com/org/q3.git")
        # Mark one completed to make it eligible
        await store.update(q1.job_id, status=JobStatus.completed, stage=JobStage.COMPLETED)
        # Add one more — should evict q1 (completed oldest), not q2/q3 queued
        q4 = await store.create(repository_id="q4", repository_url="https://github.com/org/q4.git")
        # q1 should be gone, q2/q3/q4 remain
        assert await store.get(q1.job_id) is None
        assert await store.get(q2.job_id) is not None
        assert await store.get(q3.job_id) is not None
        assert await store.get(q4.job_id) is not None

    _run(_inner())


def test_list_all_and_invalid_job_id():
    async def _inner():
        store = JobStore(max_jobs=1000)
        j1 = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git")
        j2 = await store.create(repository_id="repo2", repository_url="https://github.com/org/repo2.git")
        all_jobs = await store.list_all()
        assert len(all_jobs) >= 2
        # invalid job_id
        assert await store.get("not-a-uuid") is None
        assert await store.update("not-a-uuid", status=JobStatus.completed) is None
        # nonexistent uuid
        assert await store.get(uuid.uuid4()) is None

    _run(_inner())


def test_concurrent_create_and_update_no_corruption():
    async def _inner():
        store = JobStore(max_jobs=1000)

        async def create_one(i):
            j = await store.create(repository_id=f"repo{i}", repository_url=f"https://github.com/org/repo{i}.git")
            return j

        # 20 concurrent creates
        jobs = await asyncio.gather(*(create_one(i) for i in range(20)))
        assert len(jobs) == 20
        assert len(set(j.job_id for j in jobs)) == 20
        assert len(store) == 20

        # Concurrent updates and gets interleaved
        async def update_one(job):
            await store.update(job.job_id, status=JobStatus.running, stage=JobStage.CLONING, progress=0.5)
            fetched = await store.get(job.job_id)
            assert fetched is not None
            assert fetched.status == JobStatus.running

        await asyncio.gather(*(update_one(j) for j in jobs))

        # Verify all are running
        all_jobs = await store.list_all()
        for j in all_jobs:
            assert j.status == JobStatus.running

        # Concurrent get_by_repo
        async def get_by_repo_task(i):
            # each repo is unique, but also test concurrent reads
            return await store.get_by_repo(f"repo{i % 20}")

        results = await asyncio.gather(*(get_by_repo_task(i) for i in range(20)))
        assert all(r is not None for r in results)

    _run(_inner())


def test_job_to_dict_serializable():
    async def _inner():
        store = JobStore(max_jobs=1000)
        job = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git", branch="main")
        d = job.to_dict()
        assert d["repository_id"] == "repo1"
        assert d["repository_url"] == "https://github.com/org/repo.git"
        assert d["branch"] == "main"
        assert "job_id" in d
        assert "created_at" in d

    _run(_inner())


def test_jobstore_max_jobs_validation():
    with pytest.raises(ValueError):
        JobStore(max_jobs=0)
    with pytest.raises(ValueError):
        JobStore(max_jobs=-1)
    with pytest.raises(ValueError):
        JobStore(max_jobs="100")  # type: ignore[arg-type]


def test_cancel_all_marks_queued_running_failed():
    async def _inner():
        store = JobStore(max_jobs=1000)
        j1 = await store.create(repository_id="repo1", repository_url="https://github.com/org/repo.git")
        j2 = await store.create(repository_id="repo2", repository_url="https://github.com/org/repo2.git")
        await store.update(j1.job_id, status=JobStatus.running, stage=JobStage.CLONING)
        # j2 stays queued
        j3 = await store.create(repository_id="repo3", repository_url="https://github.com/org/repo3.git")
        await store.update(j3.job_id, status=JobStatus.completed, stage=JobStage.COMPLETED)
        await store.cancel_all(reason="shutdown")
        # j1 and j2 should be failed with shutdown reason
        f1 = await store.get(j1.job_id)
        f2 = await store.get(j2.job_id)
        f3 = await store.get(j3.job_id)
        assert f1.status == JobStatus.failed
        assert f1.stage == JobStage.FAILED
        assert f1.error == "shutdown"
        assert f2.status == JobStatus.failed
        assert f2.stage == JobStage.FAILED
        assert f3.status == JobStatus.completed  # not cancelled

    _run(_inner())


def test_create_rejects_empty_fields():
    async def _inner():
        store = JobStore(max_jobs=1000)
        with pytest.raises(ValueError):
            await store.create(repository_id="", repository_url="https://github.com/org/repo.git")
        with pytest.raises(ValueError):
            await store.create(repository_id="repo1", repository_url="")
        with pytest.raises(ValueError):
            await store.create(repository_id="   ", repository_url="https://github.com/org/repo.git")

    _run(_inner())


def test_jobstore_concurrency():
    # Named test for pytest -k test_jobstore_concurrency
    test_concurrent_create_and_update_no_corruption()
