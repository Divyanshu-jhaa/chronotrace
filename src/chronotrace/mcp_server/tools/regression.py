"""MCP tool: get_regression_risk — historical fix-commit evidence."""

from __future__ import annotations

from typing import Any

from .errors import ValidationError, map_error, to_error_envelope
from .validation import validate_file_path, validate_repository_id


def _map_regression_result(analytics_result: Any, repo_id: str, target: str) -> dict[str, Any]:
    try:
        status_obj = getattr(analytics_result, "status", None)
        status_str = getattr(status_obj, "value", str(status_obj)) if status_obj is not None else "ok"
        status_str = str(status_str)
    except Exception:
        status_str = "ok"
    if status_str == "error":
        status_str = "internal_error"
    result_obj = getattr(analytics_result, "result", None)
    evidence_obj = getattr(analytics_result, "evidence", None)
    limitations_obj = getattr(analytics_result, "limitations", None)
    fix_commits: list[dict[str, Any]] = []
    co_changed_files: list[dict[str, Any]] = []
    if result_obj is not None:
        try:
            fc = getattr(result_obj, "fix_commits", []) or []
            for c in fc:
                if hasattr(c, "hash"):
                    fix_commits.append(
                        {
                            "hash": getattr(c, "hash", ""),
                            "date": getattr(c, "date", None),
                            "message": getattr(c, "message", None),
                            "fix_reason": getattr(c, "fix_reason", None),
                        }
                    )
                elif isinstance(c, dict):
                    fix_commits.append(
                        {
                            "hash": str(c.get("hash") or c.get("commit_hash") or ""),
                            "date": c.get("date"),
                            "message": c.get("message"),
                            "fix_reason": c.get("fix_reason") or c.get("fixReason"),
                        }
                    )
        except Exception:
            pass
        try:
            rf = getattr(result_obj, "related_files", []) or []
            rf = rf[:20]
            for r in rf:
                if hasattr(r, "path"):
                    co_changed_files.append(
                        {
                            "path": getattr(r, "path", ""),
                            "count": int(getattr(r, "count", 0) or 0),
                            "confidence": float(getattr(r, "confidence", 0.0) or 0.0),
                        }
                    )
                elif isinstance(r, dict):
                    co_changed_files.append(
                        {
                            "path": str(r.get("path") or ""),
                            "count": int(r.get("count", 0) or 0),
                            "confidence": float(r.get("confidence", 0.0) or 0.0),
                        }
                    )
        except Exception:
            pass
    limitations: dict[str, Any] = {"warnings": []}
    try:
        if limitations_obj is not None:
            limitations = {
                "shallow_history": bool(getattr(limitations_obj, "shallow_history", False)),
                "commit_count": getattr(limitations_obj, "commit_count", None),
                "warnings": list(getattr(limitations_obj, "warnings", []) or []),
            }
    except Exception:
        pass
    evidence: dict[str, Any] = {"commits": [], "relationships": []}
    try:
        if evidence_obj is not None:
            commits = getattr(evidence_obj, "commits", []) or []
            ev = []
            for c in commits:
                if hasattr(c, "hash"):
                    ev.append(
                        {
                            "hash": getattr(c, "hash", ""),
                            "date": getattr(c, "date", None),
                            "message": getattr(c, "message", None),
                            "fix_reason": getattr(c, "fix_reason", None),
                        }
                    )
                elif isinstance(c, dict):
                    ev.append(c)
            evidence = {"commits": ev, "relationships": []}
    except Exception:
        pass
    envelope: dict[str, Any] = {
        "status": status_str,
        "repository_id": repo_id,
        "target": target,
        "fix_commits": fix_commits,
        "co_changed_files": co_changed_files,
        "evidence": evidence,
        "limitations": limitations,
    }
    envelope["related_files"] = co_changed_files
    if status_str in ("ok", "insufficient_history"):
        envelope["isError"] = False
    else:
        envelope["isError"] = True
    return envelope


def get_regression_risk(
    repository_id: object,
    file_path: object,
    analytics_service: Any | None = None,
    *,
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
    svc = analytics_service if analytics_service is not None else _analytics_service
    if svc is None:
        svc = globals().get("_analytics_service")
    try:
        repo_id = validate_repository_id(repository_id)
        path = validate_file_path(file_path)
    except ValidationError as ve:
        return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
    except Exception as exc:
        return map_error(ValidationError(str(exc)))
    if svc is None:
        return to_error_envelope(
            "graph_unavailable", "GRAPH_UNAVAILABLE", "Graph is temporarily unavailable; retry", is_error=True
        )
    try:
        result = None
        for meth in ("get_regression_risk", "regression_risk", "get_regression"):
            if hasattr(svc, meth):
                try:
                    result = getattr(svc, meth)(repo_id, path)  # type: ignore[misc]
                    break
                except Exception:
                    raise
        if result is None:
            return to_error_envelope("internal_error", "INTERNAL", "Internal error", is_error=True)
        return _map_regression_result(result, repo_id, path)
    except ValidationError as ve:
        return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
    except Exception as exc:
        return map_error(exc)


async def handle_get_regression_risk(
    repository_id: object,
    file_path: object,
    *,
    gateway: Any = None,
    analytics: Any = None,
    driver: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    svc = analytics if analytics is not None else globals().get("_analytics_service")
    return get_regression_risk(repository_id, file_path, analytics_service=svc)


_analytics_service: Any | None = None

__all__ = ["get_regression_risk", "handle_get_regression_risk"]
