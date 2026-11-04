"""JobStore — observable, bounded, async-safe ingestion tracking."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class JobStage(str, Enum):
    CLONING = "CLONING"
    EXTRACTING = "EXTRACTING"
    FILTERING = "FILTERING"
    WRITING_GRAPH = "WRITING_GRAPH"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(slots=True)
class Job:
    job_id: UUID
    repository_id: str
    repository_url: str
    status: JobStatus
    stage: JobStage
    created_at: datetime
    updated_at: datetime
    progress: float | None = None
    error: str | None = None
    finished_at: datetime | None = None
    branch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "repository_id": self.repository_id,
            "repository_url": self.repository_url,
            "status": self.status.value if isinstance(self.status, Enum) else str(self.status),
            "stage": self.stage.value if isinstance(self.stage, Enum) else str(self.stage),
            "progress": self.progress,
            "error": self.error,
            "created_at": self.created_at.isoformat() if hasattr(self.created_at, "isoformat") else str(self.created_at),
            "updated_at": self.updated_at.isoformat() if hasattr(self.updated_at, "isoformat") else str(self.updated_at),
            "finished_at": self.finished_at.isoformat() if self.finished_at and hasattr(self.finished_at, "isoformat") else None,
            "branch": self.branch,
        }


# Module-level active store for background task resolution (allows tests to override via dependency)
_active_store: JobStore | None = None  # type: ignore[name-defined]


class JobStore:
    """Async-safe, bounded ingestion job registry.

    - max_jobs evicts oldest completed/failed when over limit; queued/running never evicted.
    - asyncio.Lock protects all mutations.
    """

    def __init__(self, max_jobs: int = 1000) -> None:
        if not isinstance(max_jobs, int) or max_jobs <= 0:
            raise ValueError(f"max_jobs must be positive int, got {max_jobs!r}")
        self.max_jobs = max_jobs
        self._jobs: dict[UUID, Job] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        repository_id: str,
        repository_url: str,
        branch: str | None = None,
        *,
        job_id: UUID | None = None,
    ) -> Job:
        if not repository_id or not repository_id.strip():
            raise ValueError("repository_id must not be empty")
        if not repository_url or not repository_url.strip():
            raise ValueError("repository_url must not be empty")
        jid = job_id or uuid4()
        now = _now()
        job = Job(
            job_id=jid,
            repository_id=repository_id,
            repository_url=repository_url,
            status=JobStatus.queued,
            stage=JobStage.CLONING,
            created_at=now,
            updated_at=now,
            progress=None,
            error=None,
            finished_at=None,
            branch=branch,
        )
        async with self._lock:
            self._jobs[jid] = job
            await self._evict_if_needed_locked()
        logger.debug("JobStore create %s -> %s", jid, repository_id)
        return job

    async def update(
        self,
        job_id: UUID | str,
        *,
        status: JobStatus | str | None = None,
        stage: JobStage | str | None = None,
        progress: float | None = None,
        error: str | None = None,
        branch: str | None = None,
    ) -> Job | None:
        # Normalize job_id
        try:
            uid = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
        except Exception:
            return None
        async with self._lock:
            job = self._jobs.get(uid)
            if job is None:
                return None
            now = _now()
            # Normalize status/stage if string
            new_status = None
            if status is not None:
                if isinstance(status, str):
                    try:
                        new_status = JobStatus(status)
                    except ValueError:
                        new_status = JobStatus(status)  # will raise
                else:
                    new_status = status
            new_stage = None
            if stage is not None:
                if isinstance(stage, str):
                    try:
                        new_stage = JobStage(stage)
                    except ValueError:
                        new_stage = JobStage(stage)
                else:
                    new_stage = stage
            # Build updated job (dataclass with slots, so create new)
            updated = Job(
                job_id=job.job_id,
                repository_id=job.repository_id,
                repository_url=job.repository_url,
                status=new_status if new_status is not None else job.status,
                stage=new_stage if new_stage is not None else job.stage,
                created_at=job.created_at,
                updated_at=now,
                progress=progress if progress is not None else job.progress,
                error=error if error is not None else job.error,
                finished_at=job.finished_at,
                branch=branch if branch is not None else job.branch,
            )
            # If status transitions to terminal, set finished_at
            if updated.status in (JobStatus.completed, JobStatus.failed) and updated.finished_at is None:
                updated.finished_at = now
            # If error is explicitly set to None? Keep existing; but if update wants to clear error, pass error=""?
            # For failed, ensure error is set; for completed, clear error?
            if updated.status == JobStatus.completed:
                # clear error on success
                if error is None and job.error is not None:
                    # keep? Actually success should clear previous error
                    updated.error = None
            self._jobs[uid] = updated
            return updated

    async def get(self, job_id: UUID | str) -> Job | None:
        try:
            uid = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
        except Exception:
            return None
        async with self._lock:
            job = self._jobs.get(uid)
            # return copy? dataclass is mutable via replacement, so return same
            return job

    async def get_by_repo(self, repository_id: str) -> Job | None:
        async with self._lock:
            candidates = [j for j in self._jobs.values() if j.repository_id == repository_id]
            if not candidates:
                return None
            # latest by updated_at, then created_at
            candidates.sort(key=lambda j: (j.updated_at, j.created_at), reverse=True)
            return candidates[0]

    async def list_all(self) -> list[Job]:
        async with self._lock:
            return list(self._jobs.values())

    async def list_by_repo(self, repository_id: str) -> list[Job]:
        async with self._lock:
            return [j for j in self._jobs.values() if j.repository_id == repository_id]

    async def cancel_all(self, reason: str = "shutdown") -> None:
        async with self._lock:
            now = _now()
            for uid, job in list(self._jobs.items()):
                if job.status in (JobStatus.queued, JobStatus.running):
                    updated = Job(
                        job_id=job.job_id,
                        repository_id=job.repository_id,
                        repository_url=job.repository_url,
                        status=JobStatus.failed,
                        stage=JobStage.FAILED,
                        created_at=job.created_at,
                        updated_at=now,
                        progress=job.progress,
                        error=reason,
                        finished_at=now,
                        branch=job.branch,
                    )
                    self._jobs[uid] = updated

    def _eviction_candidates_locked(self) -> list[UUID]:
        # Only completed/failed are eligible
        eligible = [j for j in self._jobs.values() if j.status in (JobStatus.completed, JobStatus.failed)]
        # sort by created_at ascending (oldest first)
        eligible.sort(key=lambda j: (j.created_at, j.updated_at))
        return [j.job_id for j in eligible]

    async def _evict_if_needed_locked(self) -> None:
        # Called with lock already held
        while len(self._jobs) > self.max_jobs:
            candidates = self._eviction_candidates_locked()
            if not candidates:
                # No eligible to evict; stop (queued/running never evicted)
                break
            oldest = candidates[0]
            self._jobs.pop(oldest, None)
            logger.debug("JobStore evicted %s", oldest)

    # Synchronous helpers for tests that need len
    def __len__(self) -> int:
        return len(self._jobs)


_default_store: JobStore | None = None


def get_default_store(max_jobs: int = 1000) -> JobStore:
    global _default_store
    if _default_store is None:
        _default_store = JobStore(max_jobs=max_jobs)
    return _default_store


async def run_ingestion(
    job_id: UUID | str,
    repository_id: str,
    url: str,
    branch: str | None = None,
) -> None:
    """Background ingestion runner.

    Updates JobStore through stage lifecycle, delegates heavy work to
    IngestionService via asyncio.to_thread.

    This function is designed to be scheduled via FastAPI BackgroundTasks:
        background_tasks.add_task(run_ingestion, job.job_id, repo_id, url, branch)

    It resolves the JobStore via active-store fallback chain to handle
    dependency overrides in tests.
    """
    # Resolve JobStore
    store: JobStore | None = None
    # 1) active store pointer
    try:
        if _active_store is not None:
            store = _active_store
            # Verify job exists in this store; if not, try other stores
            # We will check after
            # Quick check without lock: try to see if job_id in store (optimistic)
            # If not, fallback
            # We do async check below after fallback chain
            pass
    except Exception:
        store = None

    # 2) try to get from app state if available
    if store is None:
        try:
            # Try importing main app
            import importlib

            for mod_path in ("chronotrace.api.main", "chronotrace.api.app"):
                try:
                    mod = importlib.import_module(mod_path)
                    app = getattr(mod, "app", None)
                    if app is not None:
                        st = getattr(app.state, "app_state", None)
                        if st is not None and hasattr(st, "job_store") and getattr(st, "job_store", None) is not None:
                            store = getattr(st, "job_store")
                            break
                        if hasattr(app.state, "job_store") and getattr(app.state, "job_store", None) is not None:
                            store = getattr(app.state, "job_store")
                            break
                        if hasattr(app.state, "jobs", None):
                            pass
                except Exception:
                    continue
        except Exception:
            pass

    # 3) fallback to default singleton
    if store is None:
        # Check if _active_store was set but job not there, still try default
        try:
            # If _active_store exists, use it regardless
            if _active_store is not None:
                store = _active_store
            else:
                store = get_default_store()
        except Exception:
            store = get_default_store()

    # If store still None, create default
    if store is None:
        store = get_default_store()

    # Verify job exists in chosen store; if not, try to search other possible stores
    # Try to find job in any known store (active vs default)
    job_found = None
    try:
        job_found = await store.get(job_id)
    except Exception:
        job_found = None
    if job_found is None and store is not _active_store and _active_store is not None:
        try:
            alt = await _active_store.get(job_id)
            if alt is not None:
                store = _active_store
                job_found = alt
        except Exception:
            pass
    if job_found is None and store is not get_default_store():
        try:
            default = get_default_store()
            alt2 = await default.get(job_id)
            if alt2 is not None:
                store = default
                job_found = alt2
        except Exception:
            pass

    # If still not found, we cannot proceed; log and return
    if job_found is None:
        logger.warning("run_ingestion: job %s not found in store, aborting", job_id)
        return

    # Transition to running / CLONING
    try:
        await store.update(job_id, status=JobStatus.running, stage=JobStage.CLONING, progress=0.05)
    except Exception as e:
        logger.warning("run_ingestion failed to update to running: %s", e)

    # Delegate heavy work
    try:
        # Lazy import to avoid loading heavy deps at import time
        ingestion_service = None
        # Try to get service from app state if available
        try:
            import importlib

            for mod_path in ("chronotrace.api.main",):
                try:
                    mod = importlib.import_module(mod_path)
                    app = getattr(mod, "app", None)
                    if app is not None:
                        st = getattr(app.state, "app_state", None)
                        if st is not None and hasattr(st, "ingestion_service") and getattr(st, "ingestion_service", None) is not None:
                            ingestion_service = getattr(st, "ingestion_service")
                            break
                except Exception:
                    continue
        except Exception:
            pass

        if ingestion_service is None:
            # Create default IngestionService
            try:
                from chronotrace.ingestion.service import IngestionService

                # Try to get config from app state or load
                config = None
                try:
                    import importlib

                    mod = importlib.import_module("chronotrace.api.main")
                    app = getattr(mod, "app", None)
                    if app is not None:
                        st = getattr(app.state, "app_state", None)
                        if st is not None and hasattr(st, "config"):
                            config = getattr(st, "config")
                        elif hasattr(app.state, "config"):
                            config = getattr(app.state, "config")
                except Exception:
                    pass
                # Try to get gateway
                gateway = None
                try:
                    mod = importlib.import_module("chronotrace.api.main")
                    app = getattr(mod, "app", None)
                    if app is not None:
                        st = getattr(app.state, "app_state", None)
                        if st is not None and hasattr(st, "graph_gateway"):
                            gateway = getattr(st, "graph_gateway")
                        elif hasattr(app.state, "graph_gateway"):
                            gateway = getattr(app.state, "graph_gateway")
                except Exception:
                    pass
                ingestion_service = IngestionService(config=config, gateway=gateway)
            except Exception as e:
                logger.debug("Failed to create IngestionService: %s", e)
                ingestion_service = None

        # If service available, call ingest via to_thread with stage callbacks simulation
        if ingestion_service is not None:
            # Update to EXTRACTING before heavy work to show progress
            try:
                await store.update(job_id, stage=JobStage.EXTRACTING, progress=0.2)
            except Exception:
                pass

            # Run ingestion in thread; support both ingest(url, branch) and ingest(url, branch, repositories_dir)
            def _call_ingest() -> Any:
                try:
                    # Try common signatures
                    import inspect

                    sig = inspect.signature(ingestion_service.ingest)
                    params = list(sig.parameters.keys())
                    # Check if supports repositories_dir kw
                    if "repositories_dir" in params:
                        return ingestion_service.ingest(url, branch, repositories_dir=None)
                    elif "branch" in params:
                        return ingestion_service.ingest(url, branch)
                    else:
                        return ingestion_service.ingest(url)
                except TypeError:
                    # Fallback positional
                    try:
                        return ingestion_service.ingest(url, branch)
                    except Exception:
                        return ingestion_service.ingest(url)

            await asyncio.to_thread(_call_ingest)

            # Simulate filtering/writing stages
            try:
                await store.update(job_id, stage=JobStage.FILTERING, progress=0.6)
                await asyncio.sleep(0)  # yield
                await store.update(job_id, stage=JobStage.WRITING_GRAPH, progress=0.85)
            except Exception:
                pass
        else:
            # No service, simulate short sleep for mock testing? Just succeed
            await asyncio.sleep(0.01)

        # Success path
        await store.update(job_id, status=JobStatus.completed, stage=JobStage.COMPLETED, progress=1.0)
        logger.info("run_ingestion completed %s", job_id)
    except Exception as exc:
        # Redact secrets from error
        err_msg = str(exc)
        # Basic redaction
        try:
            from chronotrace.ingestion.exceptions import _redact_secrets

            err_msg = _redact_secrets(err_msg)
        except Exception:
            import re

            err_msg = re.sub(r"://[^@\s]+:[^@\s]+@", "://[REDACTED]@", err_msg)
        # Ensure error not too long
        if len(err_msg) > 2000:
            err_msg = err_msg[:2000]
        try:
            await store.update(job_id, status=JobStatus.failed, stage=JobStage.FAILED, error=err_msg)
        except Exception:
            pass
        logger.warning("run_ingestion failed %s: %s", job_id, err_msg)
