"""MCP tool: get_file_history — repository-scoped commit history with pagination."""

from __future__ import annotations

from typing import Any

from .errors import ValidationError, map_error, to_error_envelope
from .validation import validate_file_path, validate_limit, validate_repository_id


def _file_exists(graph: Any, repository_id: str, canonical_path: str) -> bool | None:
    try:
        if hasattr(graph, "get_file_by_path"):
            try:
                res = graph.get_file_by_path(repository_id, canonical_path)  # type: ignore[misc]
                if res is not None:
                    return True
            except Exception:
                pass
        for attr in ("files", "file_repo", "fileRepository"):
            if hasattr(graph, attr):
                inner = getattr(graph, attr)
                if hasattr(inner, "get_by_path"):
                    try:
                        res = inner.get_by_path(repository_id, canonical_path)  # type: ignore[misc]
                        if res is not None:
                            return True
                    except Exception:
                        pass
        try:
            from chronotrace.core.analytics.repositories import get_file as _get_file  # type: ignore

            driver = getattr(graph, "driver", None) or getattr(graph, "_driver", None) or graph
            if hasattr(driver, "session"):
                try:
                    res = _get_file(driver, repository_id, canonical_path)  # type: ignore[misc]
                    if res is not None:
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        if graph is None:
            return None
        return False
    except Exception:
        return None


def get_file_history(
    repository_id: object,
    file_path: object,
    limit: object = None,
    graph_gateway: Any | None = None,
    analytics_service: Any | None = None,
    *,
    _graph_gateway: Any | None = None,
    _analytics_service: Any | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if kwargs:
        unknown = ", ".join(sorted(kwargs.keys()))
        return to_error_envelope(
            "validation_error",
            "VALIDATION_ERROR",
            f"unknown field(s): {unknown}",
            is_error=True,
        )
    gw = graph_gateway if graph_gateway is not None else _graph_gateway
    if gw is None:
        gw = globals().get("_graph_gateway")
    svc = analytics_service if analytics_service is not None else _analytics_service
    if svc is None:
        svc = globals().get("_analytics_service")
    if gw is None and svc is not None:
        try:
            gw = getattr(svc, "graph", None)
        except Exception:
            gw = None
    try:
        repo_id = validate_repository_id(repository_id)
        path = validate_file_path(file_path)
        limit_val = validate_limit(limit, default=20, max_limit=100)
    except ValidationError as ve:
        return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
    except Exception as exc:
        return map_error(ValidationError(str(exc)))
    if gw is not None:
        exists = _file_exists(gw, repo_id, path)
        if exists is False:
            return to_error_envelope(
                "not_found",
                "NOT_FOUND",
                f"file not found: {path} in repository {repo_id}",
                is_error=True,
            )
    driver: Any | None = None
    if gw is not None:
        try:
            driver = getattr(gw, "driver", None) or getattr(gw, "_driver", None) or gw
            if driver is None and hasattr(gw, "get_commits_touching_file"):
                driver = gw
        except Exception:
            driver = gw
    use_repo = False
    if driver is not None and (hasattr(driver, "session") or hasattr(driver, "get_commits_touching_file") or (hasattr(gw, "get_commits_touching_file") if gw else False)):
        use_repo = True
    if driver is not None and use_repo:
        try:
            from chronotrace.core.graph.repositories.file_history import FileHistoryRepository  # type: ignore

            repo = FileHistoryRepository(driver)
            result = repo.get_commits_for_file(repo_id, path, limit_val)
            commits = result.get("commits", []) or []
            total_count = result.get("total_count")
            has_more = result.get("has_more", False)
            limit_applied = result.get("limit_applied", limit_val)
            if not commits:
                exists2 = _file_exists(gw if gw is not None else driver, repo_id, path)
                if exists2 is False:
                    return to_error_envelope(
                        "not_found",
                        "NOT_FOUND",
                        f"file not found: {path} in repository {repo_id}",
                        is_error=True,
                    )
                if svc is not None:
                    try:
                        fallback = svc.get_file_history(repo_id, path, limit=limit_val)  # type: ignore[misc]
                        status_obj = getattr(fallback, "status", None)
                        status_str = getattr(status_obj, "value", str(status_obj)) if status_obj is not None else "ok"
                        if str(status_str) == "not_found":
                            return to_error_envelope(
                                "not_found",
                                "NOT_FOUND",
                                f"file not found: {path} in repository {repo_id}",
                                is_error=True,
                            )
                    except Exception:
                        pass
            envelope: dict[str, Any] = {
                "status": "ok",
                "repository_id": repo_id,
                "target": path,
                "commits": commits,
                "limit_applied": limit_applied,
                "isError": False,
            }
            if total_count is not None:
                envelope["total_count"] = total_count
            envelope["has_more"] = bool(has_more)
            return envelope
        except ValidationError as ve:
            return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
        except Exception as exc:
            msg = str(exc)
            if "must be a non-empty string" in msg or "limit must be" in msg:
                return map_error(ValidationError(msg))
            mapped = map_error(exc)
            if mapped.get("status") in ("graph_unavailable", "internal_error"):
                # Try analytics fallback before returning
                pass
            else:
                return mapped
    if svc is not None:
        try:
            for meth in ("get_file_history", "file_history", "get_history"):
                if hasattr(svc, meth):
                    try:
                        analytics_result = getattr(svc, meth)(repo_id, path, limit=limit_val)  # type: ignore[misc]
                    except TypeError:
                        try:
                            analytics_result = getattr(svc, meth)(repo_id, path)  # type: ignore[misc]
                        except Exception:
                            continue
                    try:
                        status_obj = getattr(analytics_result, "status", None)
                        status_str = getattr(status_obj, "value", str(status_obj)) if status_obj is not None else "ok"
                        status_str = str(status_str)
                    except Exception:
                        status_str = "ok"
                    if status_str == "error":
                        status_str = "internal_error"
                    if status_str == "not_found":
                        return to_error_envelope(
                            "not_found",
                            "NOT_FOUND",
                            f"file not found: {path} in repository {repo_id}",
                            is_error=True,
                        )
                    result_obj = getattr(analytics_result, "result", None)
                    evidence_obj = getattr(analytics_result, "evidence", None)
                    commits_list: list[dict[str, Any]] = []
                    if result_obj is not None and hasattr(result_obj, "commits"):
                        try:
                            for c in getattr(result_obj, "commits", []) or []:  # type: ignore[union-attr]
                                if hasattr(c, "hash"):
                                    commits_list.append(
                                        {
                                            "hash": getattr(c, "hash", ""),
                                            "date": getattr(c, "date", None),
                                            "message": getattr(c, "message", None),
                                            "author": None,
                                            "is_fix": False,
                                            "fix_reason": getattr(c, "fix_reason", None),
                                            "additions": None,
                                            "deletions": None,
                                            "change_type": None,
                                        }
                                    )
                                elif isinstance(c, dict):
                                    commits_list.append(
                                        {
                                            "hash": str(c.get("hash") or ""),
                                            "date": c.get("date"),
                                            "message": c.get("message"),
                                            "author": c.get("author"),
                                            "is_fix": bool(c.get("is_fix", False)),
                                            "fix_reason": c.get("fix_reason") or c.get("fixReason"),
                                            "additions": c.get("additions"),
                                            "deletions": c.get("deletions"),
                                            "change_type": c.get("change_type"),
                                        }
                                    )
                        except Exception:
                            pass
                    if not commits_list and evidence_obj is not None:
                        try:
                            for c in getattr(evidence_obj, "commits", []) or []:
                                if hasattr(c, "hash"):
                                    commits_list.append(
                                        {
                                            "hash": getattr(c, "hash", ""),
                                            "date": getattr(c, "date", None),
                                            "message": getattr(c, "message", None),
                                            "author": None,
                                            "is_fix": False,
                                            "fix_reason": getattr(c, "fix_reason", None),
                                            "additions": None,
                                            "deletions": None,
                                            "change_type": None,
                                        }
                                    )
                        except Exception:
                            pass
                    if not commits_list:
                        try:
                            from chronotrace.core.analytics.criticality import (
                                _get_commits_touching as _gct,  # type: ignore
                            )

                            graph_obj = getattr(svc, "graph", gw or driver)
                            lst = _gct(graph_obj, repo_id, path)  # type: ignore[misc]
                            for c in lst or []:
                                if isinstance(c, dict):
                                    commits_list.append(
                                        {
                                            "hash": str(c.get("hash") or ""),
                                            "date": c.get("date"),
                                            "message": c.get("message"),
                                            "author": c.get("author"),
                                            "is_fix": bool(c.get("is_fix", False)),
                                            "fix_reason": c.get("fix_reason") or c.get("fixReason"),
                                            "additions": c.get("additions"),
                                            "deletions": c.get("deletions"),
                                            "change_type": c.get("change_type"),
                                        }
                                    )
                            commits_list.sort(key=lambda x: (x.get("date") or ""), reverse=True)
                            commits_list = commits_list[:limit_val]
                        except Exception:
                            pass
                    total_count_val: int | None = None
                    try:
                        from chronotrace.core.analytics.criticality import (
                            _get_commits_touching as _gct2,  # type: ignore
                        )

                        graph_obj2 = getattr(svc, "graph", gw or driver)
                        full = _gct2(graph_obj2, repo_id, path)  # type: ignore[misc]
                        total_count_val = len(full) if isinstance(full, list) else None
                    except Exception:
                        total_count_val = None
                    has_more_val = False
                    if total_count_val is not None:
                        has_more_val = total_count_val > len(commits_list)
                    envelope: dict[str, Any] = {
                        "status": status_str if status_str in ("ok", "insufficient_history") else "ok",
                        "repository_id": repo_id,
                        "target": path,
                        "commits": commits_list,
                        "limit_applied": limit_val,
                        "isError": False,
                    }
                    if total_count_val is not None:
                        envelope["total_count"] = total_count_val
                    envelope["has_more"] = bool(has_more_val)
                    return envelope
            return to_error_envelope("internal_error", "INTERNAL", "Internal error", is_error=True)
        except ValidationError as ve:
            return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
        except Exception as exc:
            return map_error(exc)
    return to_error_envelope(
        "graph_unavailable", "GRAPH_UNAVAILABLE", "Graph is temporarily unavailable; retry", is_error=True
    )


async def handle_get_file_history(
    repository_id: object,
    file_path: object,
    limit: object = None,
    *,
    gateway: Any = None,
    analytics: Any = None,
    driver: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    gw = gateway if gateway is not None else driver
    svc = analytics
    if svc is None:
        svc = globals().get("_analytics_service")
    if gw is None:
        gw = globals().get("_graph_gateway")
    return get_file_history(repository_id, file_path, limit, graph_gateway=gw, analytics_service=svc)


_analytics_service: Any | None = None
_graph_gateway: Any | None = None

__all__ = ["get_file_history", "handle_get_file_history"]
