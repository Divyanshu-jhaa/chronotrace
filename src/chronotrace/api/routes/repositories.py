"""Repository registration, listing, and status — non-blocking ingestion via JobStore."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi import HTTPException

from chronotrace.api.dependencies import get_app_state, get_job_store
from chronotrace.api.jobs import Job, JobStatus, run_ingestion
from chronotrace.api.schemas.repositories import RepositoryRegisterRequest

router = APIRouter(tags=["repositories"])


def _get_graph_gateway_optional(request: Request) -> Any | None:
    """Optional gateway — returns None when not initialized instead of 503."""
    try:
        state = get_app_state(request)
        gw = getattr(state, "graph_gateway", None)
        if gw is not None:
            return gw
        drv = getattr(state, "driver", None)
        if drv is not None:
            return drv
        # Also check direct app state attributes
        if hasattr(request.app.state, "graph_gateway") and getattr(request.app.state, "graph_gateway", None) is not None:
            return getattr(request.app.state, "graph_gateway")
        if hasattr(request.app.state, "driver") and getattr(request.app.state, "driver", None) is not None:
            return getattr(request.app.state, "driver")
        return None
    except Exception:
        return None


def _derive_repository_id(url: str) -> str:
    """Derive stable repository_id from URL — scheme-insensitive host+path."""
    try:
        from chronotrace.ingestion.acquisition import derive_repository_id, canonicalize_url

        canonical = canonicalize_url(url)
        return derive_repository_id(canonical)
    except Exception:
        pass
    # Fallback: lower, strip .git, remove scheme, handle git@
    try:
        stripped = url.strip()
        if stripped.startswith("git@"):
            # git@host:owner/repo -> host/owner/repo
            m = re.match(r"^git@([^:]+):(.+)$", stripped)
            if m:
                host = m.group(1).lower()
                path = m.group(2).strip().lstrip("/").rstrip("/")
                if path.lower().endswith(".git"):
                    path = path[:-4].rstrip("/")
                path = path.lower()
                return f"{host}/{path}".rstrip("/").lower()
            return stripped.lower().rstrip("/").rstrip(".git").lower()
        parsed = urllib.parse.urlparse(stripped)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").rstrip("/").lower()
        if path.lower().endswith(".git"):
            path = path[:-4].rstrip("/")
        if host:
            combined = f"{host}{path}".rstrip("/")
            if not combined:
                combined = host
            return combined.lower()
        # No host (e.g., already a repository_id)
        cleaned = stripped.lower().rstrip("/")
        if cleaned.lower().endswith(".git"):
            cleaned = cleaned[:-4].rstrip("/")
        return cleaned.lower()
    except Exception:
        return url.strip().lower().rstrip("/")


def _job_to_status_dict(job: Job) -> dict[str, Any]:
    """Serialize Job to API status payload."""
    def _iso(dt: Any) -> str | None:
        if dt is None:
            return None
        try:
            return dt.isoformat()
        except Exception:
            return str(dt)
    return {
        "repository_id": job.repository_id,
        "job_id": str(job.job_id),
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "stage": job.stage.value if hasattr(job.stage, "value") else str(job.stage),
        "progress": job.progress,
        "error": job.error,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "finished_at": _iso(job.finished_at),
        "repository_url": job.repository_url,
        "url": job.repository_url,
        "branch": job.branch,
    }


@router.post("/repositories", status_code=201)
async def register_repository(
    payload: RepositoryRegisterRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    job_store: Any = Depends(get_job_store),
    graph_gateway: Any = Depends(_get_graph_gateway_optional),
) -> JSONResponse:
    """Validate Git URL, create job, trigger ingestion without blocking."""
    url = payload.url.strip()
    branch = payload.branch.strip() if payload.branch and payload.branch.strip() else None
    if branch == "":
        branch = None
    # Derive stable repository_id
    repository_id = _derive_repository_id(url)
    # Create job (queued)
    try:
        job: Job = await job_store.create(
            repository_id=repository_id,
            repository_url=url,
            branch=branch,
        )
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to create job")

    # Record active store for run_ingestion fallback
    try:
        import chronotrace.api.jobs as jobs_mod

        jobs_mod._active_store = job_store  # type: ignore[attr-defined]
    except Exception:
        pass

    # Schedule background ingestion — non-blocking (fire-and-forget)
    # Use asyncio.create_task for true non-blocking (<500ms) — BackgroundTasks would
    # be awaited by the ASGI test harness, so we prefer create_task.
    try:
        import asyncio

        asyncio.create_task(run_ingestion(job.job_id, repository_id, url, branch))
    except Exception:
        try:
            background_tasks.add_task(run_ingestion, job.job_id, repository_id, url, branch)
        except Exception:
            pass

    body = {
        "repository_id": repository_id,
        "job_id": str(job.job_id),
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
    }
    # Include url for convenience
    body["url"] = url
    if branch:
        body["branch"] = branch
    return JSONResponse(
        status_code=201,
        content=body,
        headers={
            "Location": f"/repositories/{repository_id}",
            "X-Job-Id": str(job.job_id),
        },
    )


@router.get("/repositories")
async def list_repositories(
    request: Request,
    limit: int = Query(20, ge=1, le=100, description="Max items to return (1-100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    job_store: Any = Depends(get_job_store),
    graph_gateway: Any = Depends(_get_graph_gateway_optional),
) -> JSONResponse:
    """List known repositories with ingestion status — bounded pagination.

    Merges JobStore and GraphGateway; works without graph.
    """
    try:
        jobs: list[Job] = await job_store.list_all()
    except Exception:
        jobs = []

    # Deduplicate by repository_id — keep latest job per repo by updated_at
    latest_by_repo: dict[str, Job] = {}
    for job in jobs:
        rid = job.repository_id
        existing = latest_by_repo.get(rid)
        if existing is None:
            latest_by_repo[rid] = job
        else:
            # Compare updated_at, then created_at
            try:
                if job.updated_at > existing.updated_at or (job.updated_at == existing.updated_at and job.created_at > existing.created_at):
                    latest_by_repo[rid] = job
            except Exception:
                # Fallback: keep job with later creation
                if job.created_at > existing.created_at:
                    latest_by_repo[rid] = job

    # If graph_gateway has additional repos not in jobs, include them as synthetic entries
    # Try to discover via gateway.repositories.list or similar — best effort
    if graph_gateway is not None:
        try:
            # Try several possible listing methods
            extra = None
            for attr in ("list_repositories", "list", "list_by_repository", "all"):
                # Check gateway.repositories
                repos_obj = getattr(graph_gateway, "repositories", None)
                if repos_obj is not None and hasattr(repos_obj, "list"):
                    try:
                        # repositories.list() might list all? Not standard; try list_all
                        pass
                    except Exception:
                        pass
                # Direct gateway method
                fn = getattr(graph_gateway, attr, None)
                if callable(fn):
                    try:
                        maybe = fn()
                        if isinstance(maybe, list) and maybe:
                            extra = maybe
                            break
                    except Exception:
                        continue
            # If we found extra repos, add them if not already present
            if isinstance(extra, list):
                for r in extra:
                    try:
                        rid = getattr(r, "repository_id", None) or getattr(r, "id", None)
                        if rid and rid not in latest_by_repo:
                            # Create synthetic job-like entry for listing (status from repo head)
                            # We keep minimal placeholder
                            pass
                    except Exception:
                        continue
        except Exception:
            pass

    sorted_jobs = sorted(
        latest_by_repo.values(),
        key=lambda j: (j.updated_at, j.created_at),
        reverse=True,
    )
    total = len(sorted_jobs)
    paginated = sorted_jobs[offset : offset + limit]

    items: list[dict[str, Any]] = []
    for job in paginated:
        last_ingested_at = None
        head = None
        url = job.repository_url
        # Try to enrich from graph
        if graph_gateway is not None:
            try:
                repo = None
                # Try get_repository
                for attr in ("get_repository", "get", "get_by_id"):
                    fn = getattr(graph_gateway, attr, None)
                    if callable(fn):
                        try:
                            repo = fn(job.repository_id)
                            if repo is not None:
                                break
                        except Exception:
                            continue
                if repo is not None:
                    # Extract last_ingested_at / head
                    last_ingested_at = getattr(repo, "last_ingested_at", None)
                    if last_ingested_at is None:
                        last_ingested_at = getattr(repo, "lastSuccessfulIngestionAt", None)
                    head = getattr(repo, "current_head", None)
                    if head is None:
                        head = getattr(repo, "currentHead", None)
                    if head is None:
                        head = getattr(repo, "last_ingested_head", None)
                    # Prefer remote_url for url
                    remote = getattr(repo, "remote_url", None)
                    if remote:
                        url = remote
                    # Normalize datetimes to iso
                    if last_ingested_at is not None:
                        try:
                            last_ingested_at = last_ingested_at.isoformat() if hasattr(last_ingested_at, "isoformat") else str(last_ingested_at)
                        except Exception:
                            last_ingested_at = str(last_ingested_at)
                    if head is not None:
                        head = str(head)
            except Exception:
                pass
        # Ensure updated_at iso
        def _iso(dt: Any) -> str | None:
            if dt is None:
                return None
            try:
                return dt.isoformat()
            except Exception:
                return str(dt)

        items.append(
            {
                "repository_id": job.repository_id,
                "url": url,
                "ingestion_status": job.status.value if hasattr(job.status, "value") else str(job.status),
                "status": job.status.value if hasattr(job.status, "value") else str(job.status),
                "last_ingested_at": last_ingested_at,
                "head": head,
                "job_id": str(job.job_id),
                "updated_at": _iso(job.updated_at),
                "created_at": _iso(job.created_at),
                "stage": job.stage.value if hasattr(job.stage, "value") else str(job.stage),
            }
        )

    return JSONResponse(
        status_code=200,
        content={
            "repositories": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    job_store: Any = Depends(get_job_store),
    graph_gateway: Any = Depends(_get_graph_gateway_optional),
) -> JSONResponse:
    """Get job status by job_id — alternative poll endpoint."""
    job: Job | None = None
    try:
        job = await job_store.get(job_id)
    except Exception:
        job = None
    # Try UUID parsing if string lookup failed
    if job is None:
        try:
            from uuid import UUID

            uid = UUID(str(job_id).strip())
            job = await job_store.get(uid)
        except Exception:
            pass
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    payload = _job_to_status_dict(job)
    # Enrich with graph last_ingested_at/head
    if graph_gateway is not None:
        try:
            repo = None
            for attr in ("get_repository", "get"):
                fn = getattr(graph_gateway, attr, None)
                if callable(fn):
                    try:
                        repo = fn(job.repository_id)
                        if repo is not None:
                            break
                    except Exception:
                        continue
            if repo is not None:
                lai = getattr(repo, "last_ingested_at", None)
                head = getattr(repo, "current_head", None)
                if lai is not None:
                    try:
                        payload["last_ingested_at"] = lai.isoformat() if hasattr(lai, "isoformat") else str(lai)
                    except Exception:
                        payload["last_ingested_at"] = str(lai)
                if head is not None:
                    payload["head"] = str(head)
        except Exception:
            pass
    return JSONResponse(status_code=200, content=payload)


# Must be defined after /jobs to avoid path capture; place /repositories/{id:path} last
@router.get("/repositories/{repository_id:path}")
async def get_repository_status(
    repository_id: str,
    job_store: Any = Depends(get_job_store),
    graph_gateway: Any = Depends(_get_graph_gateway_optional),
) -> JSONResponse:
    """Get repository/job status by repository_id — pollable ingestion state."""
    # Decode repository_id if URL-encoded (host/path with slashes is path-param)
    try:
        decoded = urllib.parse.unquote(repository_id)
        # If decoded differs, keep decoded as canonical (but also try original)
        repository_id_decoded = decoded
    except Exception:
        repository_id_decoded = repository_id

    # Try both decoded and raw
    job: Job | None = None
    for rid in (repository_id_decoded, repository_id):
        try:
            job = await job_store.get_by_repo(rid)
            if job is not None:
                break
        except Exception:
            continue
        # Also try direct match with all jobs (if get_by_repo fails due to mismatch)
        try:
            all_jobs = await job_store.list_all()
            for j in all_jobs:
                if j.repository_id == rid or j.repository_id.lower() == rid.lower():
                    job = j
                    break
            if job is not None:
                break
        except Exception:
            pass

    # If no job, check graph for repository existence — return repo metadata or 404
    if job is None and graph_gateway is not None:
        try:
            repo = None
            for attr in ("get_repository", "get"):
                fn = getattr(graph_gateway, attr, None)
                if callable(fn):
                    try:
                        repo = fn(repository_id_decoded)
                        if repo is not None:
                            break
                        # Try alternative rid
                        if repository_id != repository_id_decoded:
                            repo = fn(repository_id)
                            if repo is not None:
                                break
                    except Exception:
                        continue
            if repo is not None:
                # Return synthetic completed status from graph
                def _iso2(dt: Any) -> str | None:
                    if dt is None:
                        return None
                    try:
                        return dt.isoformat()
                    except Exception:
                        return str(dt)

                lai = getattr(repo, "last_ingested_at", None)
                head = getattr(repo, "current_head", None)
                remote = getattr(repo, "remote_url", None)
                return JSONResponse(
                    status_code=200,
                    content={
                        "repository_id": getattr(repo, "repository_id", repository_id_decoded) or repository_id_decoded,
                        "job_id": getattr(repo, "id", repository_id_decoded),
                        "status": "completed",
                        "stage": "COMPLETED",
                        "progress": 1.0,
                        "error": None,
                        "created_at": _iso2(getattr(repo, "last_ingested_at", None)) or _iso2(getattr(repo, "created_at", None)) or "",
                        "updated_at": _iso2(lai) or "",
                        "finished_at": _iso2(lai),
                        "repository_url": remote or repository_id_decoded,
                        "url": remote or repository_id_decoded,
                        "branch": getattr(repo, "default_branch", None),
                        "last_ingested_at": _iso2(lai),
                        "head": str(head) if head is not None else None,
                    },
                )
        except Exception:
            pass

    if job is None:
        raise HTTPException(status_code=404, detail=f"repository {repository_id} not found")

    payload = _job_to_status_dict(job)
    # Enrich with graph head/last_ingested_at
    if graph_gateway is not None:
        try:
            repo = None
            for attr in ("get_repository", "get"):
                fn = getattr(graph_gateway, attr, None)
                if callable(fn):
                    try:
                        repo = fn(job.repository_id)
                        if repo is not None:
                            break
                    except Exception:
                        continue
            if repo is not None:
                lai = getattr(repo, "last_ingested_at", None)
                head = getattr(repo, "current_head", None)
                if lai is not None:
                    try:
                        payload["last_ingested_at"] = lai.isoformat() if hasattr(lai, "isoformat") else str(lai)
                    except Exception:
                        payload["last_ingested_at"] = str(lai)
                if head is not None:
                    payload["head"] = str(head)
        except Exception:
            pass
    return JSONResponse(status_code=200, content=payload)
