"""MCP tool: get_historical_impact — importers, dependencies, co-change, hidden couplings."""

from __future__ import annotations

from typing import Any

from .errors import ValidationError, map_error, to_error_envelope
from .validation import validate_file_path, validate_repository_id

_HIDDEN_NOTE_SUBSTR = "observed association not proven dependency"


def _to_path_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    out: list[str] = []
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                p = item.get("path") or item.get("canonical_path") or item.get("current_path") or ""
                if p:
                    out.append(str(p))
            elif hasattr(item, "path"):
                try:
                    out.append(str(item.path))
                except Exception:
                    pass
    return out


def _map_impact_result(analytics_result: Any, repo_id: str, target: str) -> dict[str, Any]:
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
    importers: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    co_changed: list[dict[str, Any]] = []
    hidden_couplings: list[dict[str, Any]] = []
    if result_obj is not None:
        try:
            raw_importers = getattr(result_obj, "importers", []) or []
            for p in _to_path_list(raw_importers):
                importers.append({"path": p, "relationship_type": "IMPORTS", "commits": []})
        except Exception:
            pass
        try:
            raw_deps = getattr(result_obj, "dependencies", []) or []
            for p in _to_path_list(raw_deps):
                dependencies.append({"path": p, "relationship_type": "IMPORTS", "commits": []})
        except Exception:
            pass
        try:
            raw_co = getattr(result_obj, "co_changed", []) or []
            for entry in raw_co:
                if hasattr(entry, "path"):
                    path_val = getattr(entry, "path", "")
                    count_val = getattr(entry, "count", None)
                    conf_val = getattr(entry, "confidence", None)
                    weight_val = getattr(entry, "weight", None)
                    hidden = bool(getattr(entry, "potential_hidden_coupling", False))
                    note_val = getattr(entry, "note", None)
                    commits_val = getattr(entry, "supporting_commits", []) or []
                    sup_commits = []
                    for c in commits_val:
                        if hasattr(c, "hash"):
                            sup_commits.append(
                                {
                                    "hash": getattr(c, "hash", ""),
                                    "date": getattr(c, "date", None),
                                    "message": getattr(c, "message", None),
                                    "fix_reason": getattr(c, "fix_reason", None),
                                }
                            )
                        elif isinstance(c, dict):
                            sup_commits.append(
                                {
                                    "hash": str(c.get("hash") or ""),
                                    "date": c.get("date"),
                                    "message": c.get("message"),
                                    "fix_reason": c.get("fix_reason") or c.get("fixReason"),
                                }
                            )
                    co_entry: dict[str, Any] = {
                        "path": path_val,
                        "relationship_type": "CO_CHANGED_WITH",
                        "count": int(count_val) if isinstance(count_val, (int, float)) else 0,
                        "confidence": float(conf_val) if isinstance(conf_val, (int, float)) else None,
                        "weight": float(weight_val) if isinstance(weight_val, (int, float)) else None,
                        "potential_hidden_coupling": hidden,
                        "commits": sup_commits,
                    }
                    if note_val:
                        co_entry["note"] = str(note_val)
                    elif hidden:
                        co_entry["note"] = _HIDDEN_NOTE_SUBSTR
                    co_changed.append(co_entry)
                elif isinstance(entry, dict):
                    path_val = entry.get("path") or entry.get("canonical_path") or ""
                    co_changed.append(
                        {
                            "path": str(path_val),
                            "relationship_type": entry.get("relationship_type", "CO_CHANGED_WITH"),
                            "count": entry.get("count", 0),
                            "confidence": entry.get("confidence"),
                            "potential_hidden_coupling": bool(entry.get("potential_hidden_coupling", False)),
                            "note": entry.get("note"),
                            "commits": entry.get("commits") or entry.get("supporting_commits") or [],
                        }
                    )
        except Exception:
            pass
        try:
            raw_hidden = getattr(result_obj, "hidden_couplings", []) or []
            for entry in raw_hidden:
                if hasattr(entry, "path"):
                    path_val = getattr(entry, "path", "")
                    count_val = getattr(entry, "count", None)
                    conf_val = getattr(entry, "confidence", None)
                    weight_val = getattr(entry, "weight", None)
                    note_val = getattr(entry, "note", None)
                    commits_val = getattr(entry, "supporting_commits", []) or []
                    sup_commits = []
                    for c in commits_val:
                        if hasattr(c, "hash"):
                            sup_commits.append(
                                {
                                    "hash": getattr(c, "hash", ""),
                                    "date": getattr(c, "date", None),
                                    "message": getattr(c, "message", None),
                                    "fix_reason": getattr(c, "fix_reason", None),
                                }
                            )
                        elif isinstance(c, dict):
                            sup_commits.append(c)
                    hidden_entry: dict[str, Any] = {
                        "path": path_val,
                        "relationship_type": "CO_CHANGED_WITH",
                        "count": int(count_val) if isinstance(count_val, (int, float)) else 0,
                        "confidence": float(conf_val) if isinstance(conf_val, (int, float)) else None,
                        "weight": float(weight_val) if isinstance(weight_val, (int, float)) else None,
                        "potential_hidden_coupling": True,
                        "note": str(note_val) if note_val else _HIDDEN_NOTE_SUBSTR,
                        "commits": sup_commits,
                    }
                    hidden_couplings.append(hidden_entry)
                elif isinstance(entry, dict):
                    note_v = entry.get("note") or _HIDDEN_NOTE_SUBSTR
                    if _HIDDEN_NOTE_SUBSTR not in str(note_v):
                        note_v = _HIDDEN_NOTE_SUBSTR
                    hidden_couplings.append(
                        {
                            "path": str(entry.get("path") or ""),
                            "relationship_type": entry.get("relationship_type", "CO_CHANGED_WITH"),
                            "count": entry.get("count", 0),
                            "confidence": entry.get("confidence"),
                            "potential_hidden_coupling": True,
                            "note": str(note_v),
                            "commits": entry.get("commits") or entry.get("supporting_commits") or [],
                        }
                    )
        except Exception:
            pass
        if not hidden_couplings and co_changed:
            for e in co_changed:
                if e.get("potential_hidden_coupling"):
                    note_v = e.get("note") or _HIDDEN_NOTE_SUBSTR
                    if _HIDDEN_NOTE_SUBSTR not in str(note_v):
                        note_v = _HIDDEN_NOTE_SUBSTR
                    hidden_couplings.append(
                        {
                            "path": e.get("path"),
                            "relationship_type": e.get("relationship_type", "CO_CHANGED_WITH"),
                            "count": e.get("count"),
                            "confidence": e.get("confidence"),
                            "weight": e.get("weight"),
                            "potential_hidden_coupling": True,
                            "note": str(note_v),
                            "commits": e.get("commits", []),
                        }
                    )
    CAP = 50
    importers = importers[:CAP]
    dependencies = dependencies[:CAP]
    co_changed = co_changed[:CAP]
    hidden_couplings = hidden_couplings[:CAP]
    for h in hidden_couplings:
        note_v = h.get("note") or ""
        if _HIDDEN_NOTE_SUBSTR not in str(note_v):
            h["note"] = _HIDDEN_NOTE_SUBSTR
            h["potential_hidden_coupling"] = True
    evidence: dict[str, Any] = {"commits": [], "relationships": []}
    try:
        if evidence_obj is not None:
            commits = getattr(evidence_obj, "commits", []) or []
            rels = getattr(evidence_obj, "relationships", []) or []
            ev_commits = []
            for c in commits:
                if hasattr(c, "hash"):
                    ev_commits.append({"hash": getattr(c, "hash", ""), "date": getattr(c, "date", None), "message": getattr(c, "message", None), "fix_reason": getattr(c, "fix_reason", None)})
                elif isinstance(c, dict):
                    ev_commits.append(c)
            ev_rels = []
            for r in rels:
                if hasattr(r, "source"):
                    ev_rels.append({"source": getattr(r, "source", ""), "target": getattr(r, "target", ""), "kind": getattr(r, "kind", ""), "count": getattr(r, "count", None)})
                elif isinstance(r, dict):
                    ev_rels.append(r)
            evidence = {"commits": ev_commits, "relationships": ev_rels}
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
        "importers": importers,
        "dependencies": dependencies,
        "co_changed": co_changed,
        "potential_hidden_couplings": hidden_couplings,
        "evidence": evidence,
        "limitations": limitations,
    }
    envelope["hidden_couplings"] = hidden_couplings
    if status_str in ("ok", "insufficient_history"):
        envelope["isError"] = False
    else:
        envelope["isError"] = True
    return envelope


def get_historical_impact(
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
        for meth in ("get_historical_impact", "historical_impact", "get_impact"):
            if hasattr(svc, meth):
                try:
                    result = getattr(svc, meth)(repo_id, path)  # type: ignore[misc]
                    break
                except Exception:
                    raise
        if result is None:
            return to_error_envelope("internal_error", "INTERNAL", "Internal error", is_error=True)
        return _map_impact_result(result, repo_id, path)
    except ValidationError as ve:
        return to_error_envelope("validation_error", ve.code, ve.message, is_error=True)
    except Exception as exc:
        return map_error(exc)


async def handle_get_historical_impact(
    repository_id: object,
    file_path: object,
    *,
    gateway: Any = None,
    analytics: Any = None,
    driver: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    svc = analytics if analytics is not None else globals().get("_analytics_service")
    return get_historical_impact(repository_id, file_path, analytics_service=svc)


_analytics_service: Any | None = None

__all__ = ["get_historical_impact", "handle_get_historical_impact"]
