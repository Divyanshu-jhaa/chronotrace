"""Report endpoint — thin analytics delegation without LLM."""

from __future__ import annotations

import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from chronotrace.mcp_server.tools.errors import ValidationError as McpValidationError
from chronotrace.mcp_server.tools.validation import validate_file_path, validate_repository_id

router = APIRouter(tags=["reports"])


def _get_analytics_optional(request: Request) -> Any | None:
    app = request.app
    for key, override in list(getattr(app, "dependency_overrides", {}).items()):
        name = getattr(key, "__name__", "")
        if "analytics" in name.lower():
            try:
                val = override()
                if val is not None:
                    return val
            except TypeError:
                try:
                    val = override(request)  # type: ignore[call-arg]
                    if val is not None:
                        return val
                except Exception:
                    pass
            except Exception:
                pass
    state = getattr(app.state, "app_state", None)
    if state is not None:
        for attr in ("analytics", "analytics_service", "analyticsService"):
            svc = getattr(state, attr, None)
            if svc is not None:
                return svc
        # also check alternative state key
        alt = getattr(app.state, "_app_state", None)
        if alt is not None:
            for attr in ("analytics", "analytics_service"):
                svc = getattr(alt, attr, None)
                if svc is not None:
                    return svc
    return None


def _decode_and_check_raw(path: str) -> None:
    if "\\" in path:
        raise ValueError("file_path must not contain backslash; use POSIX form with '/'")
    if "\x00" in path:
        raise ValueError("file_path must not contain NUL byte (\\x00)")
    lowered = path.lower()
    if "%00" in lowered:
        raise ValueError("file_path must not contain encoded NUL byte (%00)")
    decoded = urllib.parse.unquote(path)
    if decoded != path:
        if "\x00" in decoded:
            raise ValueError("file_path must not contain NUL byte (\\x00)")
        if "%00" in decoded.lower():
            raise ValueError("file_path must not contain encoded NUL byte (%00)")
        if "\\" in decoded:
            raise ValueError("file_path must not contain backslash; use POSIX form with '/'")
        if decoded.startswith("/"):
            raise ValueError("file_path must be repository-relative; absolute paths are not allowed")
        if ".." in decoded.split("/"):
            raise ValueError("file_path must not contain parent traversal '..' segments")
        # also check for absolute drive
        import re

        if re.match(r"^[A-Za-z]:([/\\]|$)", decoded):
            raise ValueError("file_path must be repository-relative; Windows absolute paths are not allowed")


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: str
    file_path: str

    @field_validator("repository_id")
    @classmethod
    def _check_repo(cls, v: object) -> str:
        if not isinstance(v, str):
            raise ValueError("repository_id must be a string")
        try:
            return validate_repository_id(v)
        except McpValidationError as e:
            raise ValueError(e.message) from e

    @field_validator("file_path")
    @classmethod
    def _check_path(cls, v: object) -> str:
        if not isinstance(v, str):
            raise ValueError("file_path must be a string")
        # raw pre-checks including decoded
        _decode_and_check_raw(v)
        try:
            normalized = validate_file_path(v)
        except McpValidationError as e:
            raise ValueError(e.message) from e
        # additional decoded check after normalization if percent-encoding present
        decoded = urllib.parse.unquote(v)
        if decoded != v:
            try:
                # validate decoded also; if it would pass but contains traversal hidden via encoding, already rejected above
                validate_file_path(decoded)
            except McpValidationError as e:
                raise ValueError(e.message) from e
        return normalized


def _sanitize(msg: str) -> str:
    if not isinstance(msg, str):
        msg = str(msg)
    lower = msg.lower()
    for token in ("password", "api_key", "api-key", "secret", "token"):
        if token in lower:
            import re

            try:
                msg = re.sub(
                    rf"{re.escape(token)}[\s=:]+[^\s,;]+",
                    f"{token}=[REDACTED]",
                    msg,
                    flags=re.IGNORECASE,
                )
            except Exception:
                pass
    return msg


def _serialize_result(analytics_result: Any, repo_id: str, target: str) -> dict[str, Any]:
    status_obj = getattr(analytics_result, "status", None)
    try:
        status_str = getattr(status_obj, "value", str(status_obj)) if status_obj is not None else "ok"
        status_str = str(status_str)
    except Exception:
        status_str = "ok"
    if status_str == "insufficient":
        status_str = "insufficient_history"
    if status_str == "error":
        status_str = "internal_error"

    result_obj = getattr(analytics_result, "result", None)
    evidence_obj = getattr(analytics_result, "evidence", None)
    limitations_obj = getattr(analytics_result, "limitations", None)

    result_dict: Any | None = None
    if result_obj is not None:
        # try to map criticality-style result
        try:
            score = getattr(result_obj, "score", None)
            score_val = float(score) if score is not None else None
        except Exception:
            score_val = None
        breakdown: dict[str, Any] = {}
        try:
            components = getattr(result_obj, "components", None)
            if isinstance(components, dict):
                for k, comp in components.items():
                    try:
                        breakdown[str(k)] = {
                            "raw": float(getattr(comp, "raw", 0.0)),
                            "normalized": float(getattr(comp, "normalized", 0.0)),
                            "weighted": float(getattr(comp, "weighted", 0.0)),
                            "score": float(getattr(comp, "score", 0.0)),
                        }
                    except Exception:
                        pass
        except Exception:
            breakdown = {}
        # build result dict preserving score/breakdown
        if score_val is not None or breakdown:
            result_dict = {"score": score_val, "breakdown": breakdown}
            try:
                repo_stats = getattr(result_obj, "repo_stats", None)
                if repo_stats:
                    result_dict["repo_stats"] = dict(repo_stats)
            except Exception:
                pass
            try:
                weights = getattr(result_obj, "weights", None)
                if weights:
                    result_dict["weights"] = dict(weights)
            except Exception:
                pass
            # include any other generic fields if result is dict-like
            try:
                if isinstance(result_obj, dict):
                    # merge dict result directly
                    result_dict = dict(result_obj)
                elif hasattr(result_obj, "__dataclass_fields__"):
                    # fallback to dataclass asdict-like for remaining fields
                    for f in result_obj.__dataclass_fields__:  # type: ignore[attr-defined]
                        if f not in ("score", "components", "repo_stats", "weights"):
                            try:
                                result_dict[f] = getattr(result_obj, f)
                            except Exception:
                                pass
            except Exception:
                pass
        else:
            # generic handling: try to convert dataclass/dict to jsonable
            try:
                if isinstance(result_obj, dict):
                    result_dict = dict(result_obj)
                elif hasattr(result_obj, "__dataclass_fields__"):
                    result_dict = {}
                    for f in result_obj.__dataclass_fields__:  # type: ignore[attr-defined]
                        try:
                            v = getattr(result_obj, f)
                            # simple jsonable conversion
                            if isinstance(v, dict):
                                result_dict[f] = dict(v)
                            elif isinstance(v, (list, tuple)):
                                result_dict[f] = list(v)
                            else:
                                result_dict[f] = v
                        except Exception:
                            pass
                else:
                    result_dict = {"value": str(result_obj)}
            except Exception:
                result_dict = None
            if result_dict == {}:
                result_dict = None

    # evidence
    evidence: dict[str, Any] = {"commits": [], "relationships": []}
    try:
        if evidence_obj is not None:
            commits = getattr(evidence_obj, "commits", []) or []
            rels = getattr(evidence_obj, "relationships", []) or []
            ev_commits: list[dict[str, Any]] = []
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
                    ev_commits.append(c)
            ev_rels: list[dict[str, Any]] = []
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

    # limitations
    limitations: dict[str, Any] = {"warnings": []}
    try:
        if limitations_obj is not None:
            lim: dict[str, Any] = {"warnings": list(getattr(limitations_obj, "warnings", []) or [])}
            for attr in ("shallow_history", "commit_count", "excluded_paths_count", "parser_failures_count"):
                try:
                    v = getattr(limitations_obj, attr, None)
                    if v is not None:
                        lim[attr] = v
                except Exception:
                    pass
            # also copy any other fields
            if hasattr(limitations_obj, "__dataclass_fields__"):
                for f in limitations_obj.__dataclass_fields__:  # type: ignore[attr-defined]
                    if f not in lim:
                        try:
                            lim[f] = getattr(limitations_obj, f)
                        except Exception:
                            pass
            limitations = lim
    except Exception:
        pass

    return {
        "status": status_str,
        "repository_id": repo_id,
        "target": target,
        "result": result_dict,
        "evidence": evidence,
        "limitations": limitations,
    }


def _call_analytics(analytics: Any, repo_id: str, target: str) -> Any:
    for meth in ("get_change_criticality", "criticality", "get_criticality"):
        if hasattr(analytics, meth):
            try:
                return getattr(analytics, meth)(repo_id, target)
            except TypeError:
                try:
                    return getattr(analytics, meth)(repo_id, target, None)  # type: ignore[misc]
                except Exception:
                    raise
            except Exception:
                raise
    raise RuntimeError("analytics missing get_change_criticality")


async def _handle_report(repo_id: str, target: str, analytics: Any) -> JSONResponse:
    if analytics is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "graph_unavailable", "message": "Graph is temporarily unavailable; retry"}},
        )
    try:
        analytics_result = _call_analytics(analytics, repo_id, target)
    except Exception as exc:
        from chronotrace.mcp_server.tools.errors import map_error

        mapped = map_error(exc)
        status = str(mapped.get("status", "internal_error"))
        err = mapped.get("error", {"code": "INTERNAL", "message": "Internal error"})
        # sanitize
        try:
            err["message"] = _sanitize(str(err.get("message", "")))
            err["code"] = _sanitize(str(err.get("code", "")))
        except Exception:
            pass
        if status == "graph_unavailable":
            return JSONResponse(status_code=503, content={"error": err})
        if status == "validation_error":
            return JSONResponse(status_code=422, content={"error": err})
        if status == "not_found":
            return JSONResponse(status_code=404, content={"error": err})
        return JSONResponse(status_code=500, content={"error": err})

    body = _serialize_result(analytics_result, repo_id, target)
    status_str = body.get("status", "ok")
    if status_str == "not_found":
        return JSONResponse(status_code=404, content=body)
    if status_str in ("ok", "insufficient_history"):
        return JSONResponse(status_code=200, content=body)
    if status_str == "graph_unavailable":
        return JSONResponse(status_code=503, content={"error": {"code": "graph_unavailable", "message": _sanitize(str(body.get("message", "Graph unavailable")))}})
    if status_str == "validation_error":
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": _sanitize(str(body.get("message", "Validation failed")))}})
    # fallback internal
    if status_str == "internal_error":
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": _sanitize("Internal error")}})
    return JSONResponse(status_code=200, content=body)


@router.post("/reports")
async def post_reports(payload: ReportRequest, request: Request, analytics: Any = Depends(_get_analytics_optional)) -> JSONResponse:
    # payload already validated and normalized (file_path)
    return await _handle_report(payload.repository_id, payload.file_path, analytics)


@router.get("/reports")
async def get_reports(
    request: Request,
    repository_id: str = Query(..., description="Repository identifier"),
    file_path: str | None = Query(None, description="Repository-relative POSIX path"),
    file: str | None = Query(None, description="Alias for file_path"),
    analytics: Any = Depends(_get_analytics_optional),
) -> JSONResponse:
    target_raw = file_path if file_path is not None else file
    if target_raw is None:
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "file_path is required"}})
    try:
        repo_id = validate_repository_id(repository_id)
    except McpValidationError as e:
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": _sanitize(e.message)}})
    except Exception as exc:
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": _sanitize(str(exc))}})
    try:
        _decode_and_check_raw(target_raw)
        normalized = validate_file_path(target_raw)
        decoded = urllib.parse.unquote(target_raw)
        if decoded != target_raw:
            try:
                validate_file_path(decoded)
            except McpValidationError as e:
                return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": _sanitize(e.message)}})
    except McpValidationError as e:
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": _sanitize(e.message)}})
    except ValueError as ve:
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": _sanitize(str(ve))}})
    except Exception as exc:
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": _sanitize(str(exc))}})
    return await _handle_report(repo_id, normalized, analytics)
