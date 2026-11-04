"""MCP tool: get_change_criticality — validated criticality via AnalyticsService."""

from __future__ import annotations

from typing import Any

from .errors import ValidationError, map_error, to_error_envelope
from .validation import validate_file_path, validate_repository_id


def _map_criticality_result(analytics_result: Any, repo_id: str, target: str) -> dict[str, Any]:
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
    score_val: float | None = None
    breakdown: dict[str, Any] = {}
    repo_stats: dict[str, Any] = {}
    weights: dict[str, Any] = {}
    if result_obj is not None:
        try:
            score_val = float(getattr(result_obj, "score", 0.0))
        except Exception:
            score_val = None
        try:
            components = getattr(result_obj, "components", {}) or {}
            for comp_name, comp in components.items():
                breakdown[comp_name] = {
                    "raw": float(getattr(comp, "raw", 0.0)),
                    "normalized": float(getattr(comp, "normalized", 0.0)),
                    "weighted": float(getattr(comp, "weighted", 0.0)),
                    "score": float(getattr(comp, "score", 0.0)),
                }
        except Exception:
            breakdown = {}
        try:
            repo_stats = dict(getattr(result_obj, "repo_stats", {}) or {})
        except Exception:
            repo_stats = {}
        try:
            weights = dict(getattr(result_obj, "weights", {}) or {})
        except Exception:
            weights = {}
    evidence: dict[str, Any] = {"commits": [], "relationships": []}
    try:
        if evidence_obj is not None:
            commits = getattr(evidence_obj, "commits", []) or []
            rels = getattr(evidence_obj, "relationships", []) or []
            ev_commits = []
            for c in commits:
                if hasattr(c, "hash"):
                    ev_commits.append(
                        {
                            "hash": getattr(c, "hash", ""),
                            "date": getattr(c, "date", None),
                            "message": getattr(c, "message", None),
                            "fix_reason": getattr(c, "fix_reason", None),
                        }
                    )
                elif isinstance(c, dict):
                    ev_commits.append(
                        {
                            "hash": str(c.get("hash") or c.get("id") or ""),
                            "date": c.get("date"),
                            "message": c.get("message"),
                            "fix_reason": c.get("fix_reason") or c.get("fixReason"),
                        }
                    )
            ev_rels = []
            for r in rels:
                if hasattr(r, "source"):
                    ev_rels.append(
                        {
                            "source": getattr(r, "source", ""),
                            "target": getattr(r, "target", ""),
                            "kind": getattr(r, "kind", ""),
                            "count": getattr(r, "count", None),
                            "weight": getattr(r, "weight", None),
                            "confidence": getattr(r, "confidence", None),
                        }
                    )
                elif isinstance(r, dict):
                    ev_rels.append(r)
            evidence = {"commits": ev_commits, "relationships": ev_rels}
            diag = getattr(evidence_obj, "diagnostics_ref", None)
            if diag is not None:
                evidence["diagnostics_ref"] = diag
    except Exception:
        pass
    limitations: dict[str, Any] = {"warnings": []}
    try:
        if limitations_obj is not None:
            limitations = {
                "shallow_history": bool(getattr(limitations_obj, "shallow_history", False)),
                "commit_count": getattr(limitations_obj, "commit_count", None),
                "excluded_paths_count": getattr(limitations_obj, "excluded_paths_count", None),
                "parser_failures_count": getattr(limitations_obj, "parser_failures_count", None),
                "warnings": list(getattr(limitations_obj, "warnings", []) or []),
            }
    except Exception:
        pass
    envelope: dict[str, Any] = {
        "status": status_str,
        "repository_id": repo_id,
        "target": target,
        "score": score_val,
        "breakdown": breakdown,
        "evidence": evidence,
        "limitations": limitations,
    }
    if repo_stats:
        envelope["repo_stats"] = repo_stats
    if weights:
        envelope["weights"] = weights
    if status_str in ("ok", "insufficient_history"):
        envelope["isError"] = False
    elif status_str in ("not_found", "validation_error", "graph_unavailable", "internal_error"):
        envelope["isError"] = True
    else:
        envelope["isError"] = False
    return envelope


def get_change_criticality(
    repository_id: object,
    file_path: object,
    analytics_service: Any | None = None,
    *,
    _analytics_service: Any | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if kwargs:
        # Unknown field -> validation_error (no arbitrary Cypher)
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
            "graph_unavailable",
            "GRAPH_UNAVAILABLE",
            "Graph is temporarily unavailable; retry",
            is_error=True,
        )
    try:
        result = None
        for meth in ("get_change_criticality", "criticality", "get_criticality"):
            if hasattr(svc, meth):
                try:
                    result = getattr(svc, meth)(repo_id, path)  # type: ignore[misc]
                    break
                except TypeError:
                    try:
                        result = getattr(svc, meth)(repo_id, path, None)  # type: ignore[misc]
                        break
                    except Exception:
                        continue
                except Exception:
                    raise
        if result is None:
            return to_error_envelope(
                "internal_error",
                "INTERNAL",
                "Internal error",
                is_error=True,
            )
        return _map_criticality_result(result, repo_id, path)
    except ValidationError as ve:
        return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
    except Exception as exc:
        return map_error(exc)


# Back-compat async handle and alias for server shim that used handle_* signature
async def handle_get_change_criticality(
    repository_id: object,
    file_path: object,
    *,
    gateway: Any = None,
    analytics: Any = None,
    driver: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    svc = analytics if analytics is not None else globals().get("_analytics_service")
    return get_change_criticality(repository_id, file_path, analytics_service=svc)


get_criticality = get_change_criticality
_analytics_service: Any | None = None

__all__ = ["get_change_criticality", "get_criticality", "handle_get_change_criticality"]
