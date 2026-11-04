"""MCP tool: recommend_tests — ranked candidate test files."""

from __future__ import annotations

from typing import Any

from .errors import ValidationError, map_error, to_error_envelope
from .validation import validate_file_path, validate_repository_id


def _map_test_reco_result(analytics_result: Any, repo_id: str, target: str) -> dict[str, Any]:
    try:
        status_obj = getattr(analytics_result, "status", None)
        status_str = getattr(status_obj, "value", str(status_obj)) if status_obj is not None else "ok"
        status_str = str(status_str)
    except Exception:
        status_str = "ok"
    if status_str == "error":
        status_str = "internal_error"
    result_obj = getattr(analytics_result, "result", None)
    limitations_obj = getattr(analytics_result, "limitations", None)
    candidates: list[dict[str, Any]] = []
    if result_obj is not None:
        try:
            raw_cands = getattr(result_obj, "candidates", []) or []
            raw_cands = raw_cands[:20]
            for idx, entry in enumerate(raw_cands):
                rank = idx + 1
                if hasattr(entry, "path"):
                    path_val = getattr(entry, "path", "")
                    score_val = getattr(entry, "score", 0.0)
                    reasons_val = getattr(entry, "reasons", []) or []
                    candidates.append(
                        {
                            "path": str(path_val),
                            "score": float(score_val) if isinstance(score_val, (int, float)) else 0.0,
                            "rank": rank,
                            "reasons": list(reasons_val),
                        }
                    )
                elif isinstance(entry, dict):
                    candidates.append(
                        {
                            "path": str(entry.get("path") or ""),
                            "score": float(entry.get("score", 0.0) or 0.0),
                            "rank": entry.get("rank", rank),
                            "reasons": list(entry.get("reasons") or []),
                        }
                    )
        except Exception:
            pass
    limitations: dict[str, Any] = {"warnings": []}
    try:
        if limitations_obj is not None:
            warnings = list(getattr(limitations_obj, "warnings", []) or [])
            has_disclaimer = any("no claim of real coverage" in str(w).lower() for w in warnings)
            if not has_disclaimer:
                warnings.insert(0, "no claim of real coverage — recommendations are heuristic suggestions, not measured coverage or guaranteed relevance")
            limitations = {
                "shallow_history": bool(getattr(limitations_obj, "shallow_history", False)) if hasattr(limitations_obj, "shallow_history") else False,
                "commit_count": getattr(limitations_obj, "commit_count", None) if hasattr(limitations_obj, "commit_count") else None,
                "warnings": warnings,
            }
        else:
            limitations = {"warnings": ["no claim of real coverage — recommendations are heuristic suggestions, not measured coverage or guaranteed relevance"]}
    except Exception:
        limitations = {"warnings": ["no claim of real coverage — recommendations are heuristic suggestions, not measured coverage or guaranteed relevance"]}
    if not any("no claim of real coverage" in str(w).lower() for w in limitations.get("warnings", [])):
        limitations["warnings"] = ["no claim of real coverage — recommendations are heuristic suggestions, not measured coverage or guaranteed relevance"] + list(limitations.get("warnings", []))
    envelope: dict[str, Any] = {
        "status": status_str,
        "repository_id": repo_id,
        "target": target,
        "candidates": candidates,
        "limitations": limitations,
    }
    try:
        evidence_obj = getattr(analytics_result, "evidence", None)
        if evidence_obj is not None:
            envelope["evidence"] = {
                "commits": list(getattr(evidence_obj, "commits", []) or []),
                "relationships": list(getattr(evidence_obj, "relationships", []) or []),
            }
    except Exception:
        pass
    if status_str in ("ok", "insufficient_history"):
        envelope["isError"] = False
    else:
        envelope["isError"] = True
    return envelope


def recommend_tests(
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
        return to_error_envelope("graph_unavailable", "GRAPH_UNAVAILABLE", "Graph is temporarily unavailable; retry", is_error=True)
    try:
        result = None
        for meth in ("recommend_tests", "get_test_recommendations", "recommend"):
            if hasattr(svc, meth):
                try:
                    result = getattr(svc, meth)(repo_id, path)  # type: ignore[misc]
                    break
                except Exception:
                    raise
        if result is None:
            return to_error_envelope("internal_error", "INTERNAL", "Internal error", is_error=True)
        return _map_test_reco_result(result, repo_id, path)
    except ValidationError as ve:
        return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
    except Exception as exc:
        return map_error(exc)


async def handle_recommend_tests(
    repository_id: object,
    file_path: object,
    *,
    gateway: Any = None,
    analytics: Any = None,
    driver: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    svc = analytics if analytics is not None else globals().get("_analytics_service")
    return recommend_tests(repository_id, file_path, analytics_service=svc)


get_recommended_tests = recommend_tests
_analytics_service: Any | None = None

__all__ = ["recommend_tests", "handle_recommend_tests"]
