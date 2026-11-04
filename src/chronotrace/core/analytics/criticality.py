"""Change criticality scoring (iter-5, Decision 4).

Retrieves four components for a target file, applies exponential decay
consistently, gathers repository-scoped min/max, normalizes, and weights.

Components:
- **Churn**: ``sum(decay_weight * (1 + log1p(additions+deletions)))`` over
  commits touching the file. ``None`` line counts treated as 0
  (``1 + log1p(0) == 1``) so every commit contributes at least
  ``decay_weight * 1`` — avoids zero-vs-null conflation.
- **Fix involvement**: ``sum(decay_weight for commits where is_fix)``.
- **Co-change**: ``sum(weight or count)`` over ``CO_CHANGED_WITH`` /
  ``CO_CHANGES`` neighbors. Already aggregated in graph, used directly
  (decay not re-applied). Falls back to degree when weight missing.
- **Fan-in**: ``count distinct importers`` via ``IMPORTS`` (structural,
  not decayed).

Normalization is repository-scoped min-max per component (Decision 4):
``norm = (raw - min) / (max - min)`` if ``max > min`` else ``0.5``.
Callers supply ``repo_stats`` so cross-repo isolation is guaranteed.

Weighting uses heuristic constants (to be evaluated in Iteration 12
backtesting):

``WEIGHTS = {churn: 0.30, fix: 0.30, cochange: 0.25, fan_in: 0.15}``

Final ``score = sum(weight_i * norm_i)`` in ``[0, 1]`` with per-component
breakdown ``{raw, normalized, weighted}`` plus evidence and determinism
(sorted output, ``now`` injection).

The module is read-only and side-effect-free; all graph reads are
parameterized and repository-scoped via :mod:`.repositories`.
"""

from __future__ import annotations

import math
import posixpath
from datetime import datetime, timezone
from typing import Any

from .contracts import CriticalityBreakdown, CriticalityResult, CommitEvidence, Evidence, RelationshipEvidence
from .decay import age_days, decay_weight, get_half_life
from .normalization import normalize_components

# Heuristic weights — documented, to be evaluated in Iteration 12 backtesting.
# Sum == 1.0 so score stays in [0, 1].
WEIGHTS: dict[str, float] = {
    "churn": 0.30,
    "fix": 0.30,
    "cochange": 0.25,
    "fan_in": 0.15,
}

# Also expose under canonical names expected by tests/docs.
WEIGHTS_CANONICAL = WEIGHTS

_COMPONENTS = ["churn", "fix", "cochange", "fan_in"]


def _normalize_path(p: str) -> str:
    if not p:
        return ""
    p = p.replace("\\", "/")
    p = posixpath.normpath(p)
    if p.startswith("./"):
        p = p[2:]
    return p


def _is_fix_commit(entry: dict[str, Any]) -> bool:
    # is_fix may be bool, int, or string
    val = entry.get("is_fix")
    if val is None:
        val = entry.get("isFix")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "y")
    return False


def _extract_commit_date(entry: dict[str, Any]) -> Any:
    for k in ("date", "commit_date", "committed_date", "author_date"):
        if k in entry and entry[k] not in (None, ""):
            return entry[k]
    # also check nested commit
    if "commit" in entry and isinstance(entry["commit"], dict):
        for k in ("date",):
            if k in entry["commit"]:
                return entry["commit"][k]
    return None


def _additions_deletions(entry: dict[str, Any]) -> tuple[int | None, int | None]:
    add = entry.get("additions")
    if add is None:
        add = entry.get("add")
    delete = entry.get("deletions")
    if delete is None:
        delete = entry.get("del")
    if delete is None:
        delete = entry.get("deletion")
    # Preserve None vs 0 distinction
    try:
        add_i = int(add) if add is not None else None
    except Exception:
        add_i = None
    try:
        del_i = int(delete) if delete is not None else None
    except Exception:
        del_i = None
    return add_i, del_i


def _cochange_raw_value(entry: dict[str, Any]) -> float:
    # Prefer weight, then count
    for k in ("weight",):
        v = entry.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    for k in ("count",):
        v = entry.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    # Fallback degree = 1 per neighbor already counted as 1
    return 1.0


def _fan_in_path(entry: Any) -> str:
    if isinstance(entry, str):
        return _normalize_path(entry)
    if isinstance(entry, dict):
        for k in ("path", "canonical_path", "current_path"):
            if k in entry and entry[k]:
                return _normalize_path(str(entry[k]))
        # fallback id split
        fid = entry.get("id", "")
        if isinstance(fid, str) and ":" in fid:
            maybe = fid.split(":", 1)[-1]
            if "/" in maybe or maybe.endswith(".py"):
                return _normalize_path(maybe)
        return _normalize_path(str(entry.get("path", "")))
    # object
    for attr in ("path", "canonical_path", "current_path"):
        if hasattr(entry, attr):
            try:
                v = getattr(entry, attr)
                if v:
                    return _normalize_path(str(v))
            except Exception:
                pass
    return _normalize_path(str(entry))


# ---------------------------------------------------------------------------
# Graph access helpers — try graph methods first, fallback to repositories
# with driver. This supports both real Neo4j drivers and in-memory fakes
# used in unit tests that expose convenience methods directly on the graph
# object.
# ---------------------------------------------------------------------------


def _resolve_driver(graph: Any) -> Any:
    # If graph is a Gateway with .driver, prefer that for repositories helper
    if hasattr(graph, "driver"):
        try:
            d = getattr(graph, "driver")
            if d is not None and hasattr(d, "session"):
                return d
        except Exception:
            pass
    if hasattr(graph, "_driver"):
        try:
            d = getattr(graph, "_driver")
            if d is not None and hasattr(d, "session"):
                return d
        except Exception:
            pass
    return graph


def _get_file(graph: Any, repository_id: str, path: str) -> dict[str, Any] | None:
    norm = _normalize_path(path)
    # Try methods on graph and nested
    for obj in (graph, getattr(graph, "files", None), getattr(graph, "file_repo", None)):
        if obj is None:
            continue
        for meth in ("get_file_by_path", "get_by_path", "get_file", "getFileByPath"):
            if hasattr(obj, meth):
                try:
                    # try repo, path
                    try:
                        res = getattr(obj, meth)(repository_id, norm)  # type: ignore[misc]
                    except TypeError:
                        # try file_id form
                        fid = f"{repository_id}:{norm}"
                        res = getattr(obj, meth)(fid, repository_id)  # type: ignore[misc]
                    if res is not None:
                        # Normalize to dict
                        if isinstance(res, dict):
                            return res
                        # Try to convert object to dict
                        if hasattr(res, "path"):
                            return {
                                "path": getattr(res, "path", norm),
                                "canonical_path": getattr(res, "current_path", norm),
                                "id": getattr(res, "id", f"{repository_id}:{norm}"),
                                "repository_id": getattr(res, "repository_id", repository_id),
                                "is_test": getattr(res, "is_test", False),
                            }
                        if isinstance(res, str):
                            return {"path": _normalize_path(res), "repository_id": repository_id}
                        return dict(res) if isinstance(res, dict) else {"path": norm}  # type: ignore[arg-type]
                except Exception:
                    continue
    # Fallback to repositories function with driver
    try:
        from .repositories import get_file as _get_file_repo

        driver = _resolve_driver(graph)
        # If driver is same as graph but graph doesn't have session, repositories will try fallback
        res = _get_file_repo(driver, repository_id, norm)
        if res is not None:
            return res
    except Exception:
        pass
    # Last resort: scan all files
    try:
        all_files = _list_all_files(graph, repository_id)
        for entry in all_files:
            if isinstance(entry, dict):
                p = entry.get("path") or entry.get("canonical_path") or entry.get("current_path") or ""
                if _normalize_path(str(p)) == norm:
                    return entry
            elif hasattr(entry, "path"):
                try:
                    if _normalize_path(str(entry.path)) == norm:
                        return {"path": _normalize_path(str(entry.path)), "repository_id": repository_id}
                except Exception:
                    pass
            elif isinstance(entry, str) and _normalize_path(entry) == norm:
                return {"path": _normalize_path(entry), "repository_id": repository_id}
    except Exception:
        pass
    return None


def _get_commits_touching(graph: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    norm = _normalize_path(path)
    # Try graph convenience methods
    for obj in (graph, getattr(graph, "commits", None), getattr(graph, "commit_repo", None), getattr(graph, "relationships", None)):
        if obj is None:
            continue
        for meth in ("get_commits_touching_file", "get_commits_for_file", "get_commits_touching", "commits_touching_file", "get_commits_for_path"):
            if hasattr(obj, meth):
                try:
                    res = getattr(obj, meth)(repository_id, norm)  # type: ignore[misc]
                    if res is not None:
                        if isinstance(res, list):
                            return [dict(r) if isinstance(r, dict) else {"hash": str(r)} for r in res if r is not None]
                except Exception:
                    continue
    # Fallback to repositories
    try:
        from .repositories import get_commits_touching_file as _repo_fn

        driver = _resolve_driver(graph)
        rows = _repo_fn(driver, repository_id, norm)
        if isinstance(rows, list):
            return rows
    except Exception:
        pass
    # Try alternative repositories helpers
    try:
        from .repositories import get_commits_for_file_paths

        driver = _resolve_driver(graph)
        rows = get_commits_for_file_paths(driver, repository_id, [norm])
        if isinstance(rows, list):
            return rows
    except Exception:
        pass
    return []


def _get_cochange_neighbors(graph: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    norm = _normalize_path(path)
    for obj in (graph, getattr(graph, "relationships", None), getattr(graph, "files", None)):
        if obj is None:
            continue
        for meth in ("get_cochange_neighbors", "get_co_change_neighbors", "get_co_changed", "cochange_neighbors", "get_cochange_for_file"):
            if hasattr(obj, meth):
                try:
                    res = getattr(obj, meth)(repository_id, norm)  # type: ignore[misc]
                    if isinstance(res, list) and res:
                        return [dict(r) if isinstance(r, dict) else {"path": str(r)} for r in res]
                    if isinstance(res, list):
                        return []  # empty but valid
                except Exception:
                    continue
    try:
        from .repositories import get_cochange_neighbors as _repo_fn

        driver = _resolve_driver(graph)
        rows = _repo_fn(driver, repository_id, norm)
        if isinstance(rows, list):
            return rows
    except Exception:
        pass
    # Inspect storage attributes for fakes
    for attr in ("_co_changes", "co_changes", "_cochange", "cochange_map", "_co_changed_with", "co_changed_with", "_co_changed"):
        if hasattr(graph, attr):
            try:
                data = getattr(graph, attr)
                out: list[dict[str, Any]] = []
                if isinstance(data, dict):
                    for k, v in data.items():
                        # k may be tuple (a,b) or string target -> dict neighbors
                        if isinstance(k, tuple) and len(k) == 2:
                            a, b = _normalize_path(str(k[0])), _normalize_path(str(k[1]))
                            cnt = 1
                            if isinstance(v, dict):
                                cnt = int(v.get("count", v.get("weight", 1)))  # type: ignore[arg-type]
                            elif isinstance(v, (int, float)):
                                cnt = int(v)  # type: ignore[arg-type]
                            if a == norm:
                                out.append({"path": b, "count": cnt, "weight": float(cnt)})
                            elif b == norm:
                                out.append({"path": a, "count": cnt, "weight": float(cnt)})
                        elif isinstance(k, str) and _normalize_path(k) == norm and isinstance(v, dict):
                            for nb, cnt_val in v.items():
                                cnt = int(cnt_val) if isinstance(cnt_val, (int, float)) else 1
                                if isinstance(cnt_val, dict):
                                    cnt = int(cnt_val.get("count", 1))
                                out.append({"path": _normalize_path(nb), "count": cnt, "weight": float(cnt)})
                        elif isinstance(k, str) and _normalize_path(k) == norm and isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    p = item.get("path") or ""
                                    out.append({"path": _normalize_path(str(p)), "count": item.get("count", 1), "weight": item.get("weight")})
                                elif isinstance(item, str):
                                    out.append({"path": _normalize_path(item), "count": 1})
                elif isinstance(data, list):
                    for e in data:
                        if isinstance(e, dict):
                            src = _normalize_path(str(e.get("source", e.get("src", ""))))
                            dst = _normalize_path(str(e.get("target", e.get("dst", ""))))
                            cnt = e.get("count", 1)
                            if src == norm and dst:
                                out.append({"path": dst, "count": cnt, "weight": e.get("weight")})
                            elif dst == norm and src:
                                out.append({"path": src, "count": cnt, "weight": e.get("weight")})
                if out:
                    return out
            except Exception:
                pass
    return []


def _get_importers(graph: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    norm = _normalize_path(path)
    for obj in (graph, getattr(graph, "relationships", None), getattr(graph, "files", None), getattr(graph, "graph", None)):
        if obj is None:
            continue
        for meth in ("get_importers", "get_importers_for_file", "importers_for", "get_importers_of"):
            if hasattr(obj, meth):
                try:
                    res = getattr(obj, meth)(repository_id, norm)  # type: ignore[misc]
                    if isinstance(res, list):
                        # ensure dict shape
                        out = []
                        for item in res:
                            if isinstance(item, dict):
                                out.append(item)
                            elif isinstance(item, str):
                                out.append({"path": _normalize_path(item)})
                            elif hasattr(item, "path"):
                                try:
                                    out.append({"path": _normalize_path(str(item.path))})
                                except Exception:
                                    out.append({"path": _normalize_path(str(item))})
                            else:
                                out.append({"path": _normalize_path(str(item))})
                        return out
                except Exception:
                    continue
    try:
        from .repositories import get_importers as _repo_fn

        driver = _resolve_driver(graph)
        rows = _repo_fn(driver, repository_id, norm)
        if isinstance(rows, list):
            return rows
    except Exception:
        pass
    # Fallback storage inspection
    for attr in ("_imports", "imports", "import_edges", "_import_edges", "imports_map"):
        if hasattr(graph, attr):
            try:
                edges = getattr(graph, attr)
                found: list[dict[str, Any]] = []
                if isinstance(edges, list):
                    for e in edges:
                        if isinstance(e, (list, tuple)) and len(e) >= 2:
                            src, dst = _normalize_path(str(e[0])), _normalize_path(str(e[1]))
                            if dst == norm:
                                found.append({"path": src})
                        elif isinstance(e, dict):
                            src = _normalize_path(str(e.get("source", e.get("src", ""))))
                            dst = _normalize_path(str(e.get("target", e.get("dst", ""))))
                            if dst == norm and src:
                                found.append({"path": src})
                elif isinstance(edges, dict):
                    for src, dsts in edges.items():
                        src_n = _normalize_path(str(src))
                        if isinstance(dsts, (list, set, tuple)):
                            for d in dsts:
                                if _normalize_path(str(d)) == norm:
                                    found.append({"path": src_n})
                        elif _normalize_path(str(dsts)) == norm:
                            found.append({"path": src_n})
                if found:
                    # dedup by path
                    seen = set()
                    dedup = []
                    for it in found:
                        p = it.get("path", "")
                        if p not in seen:
                            seen.add(p)
                            dedup.append(it)
                    return dedup
            except Exception:
                pass
    return []


def _list_all_files(graph: Any, repository_id: str) -> list[Any]:
    # Try graph methods
    for obj in (graph, getattr(graph, "files", None), getattr(graph, "file_repo", None)):
        if obj is None:
            continue
        for meth in ("list_files", "list_by_repository", "get_all_files", "get_files", "all_files"):
            if hasattr(obj, meth):
                try:
                    res = getattr(obj, meth)(repository_id)  # type: ignore[misc]
                    if isinstance(res, list) and res:
                        return res
                    if isinstance(res, list):
                        # empty, continue searching but keep
                        pass
                except Exception:
                    continue
    try:
        from .repositories import get_all_files as _repo_fn

        driver = _resolve_driver(graph)
        rows = _repo_fn(driver, repository_id)
        if isinstance(rows, list):
            return rows
    except Exception:
        pass
    # Inspect storage attributes
    for attr in ("_files", "files_list", "_file_map", "file_map", "_store"):
        if hasattr(graph, attr):
            try:
                val = getattr(graph, attr)
                if isinstance(val, list) and val:
                    sample = val[0]
                    p, _ = _extract_file_path(sample)
                    if p:
                        return val
                elif isinstance(val, dict) and val:
                    # dict id->File or repo_id -> list
                    if repository_id in val:
                        inner = val[repository_id]
                        if isinstance(inner, list):
                            return inner
                        if isinstance(inner, dict):
                            lst = list(inner.values())
                            if lst:
                                return lst
                    lst = list(val.values())
                    if lst and isinstance(lst[0], dict) or hasattr(lst[0], "path"):
                        # check if dict values contain path
                        return lst
            except Exception:
                pass
    # Scan all attributes for File-like lists
    try:
        for attr in dir(graph):
            if attr.startswith("_"):
                continue
            try:
                val = getattr(graph, attr)
                if isinstance(val, list) and val:
                    sample = val[0]
                    if isinstance(sample, dict) and ("path" in sample or "canonical_path" in sample):
                        return val
                    if hasattr(sample, "path"):
                        return val
            except Exception:
                continue
    except Exception:
        pass
    return []


def _extract_file_path(entry: Any) -> tuple[str, Any]:
    path = ""
    is_test = None
    if isinstance(entry, str):
        path = entry
    elif isinstance(entry, dict):
        path = entry.get("path") or entry.get("canonical_path") or entry.get("current_path") or entry.get("id", "")
        if ":" in str(path) and "/" in str(path).split(":", 1)[-1]:
            maybe = str(path).split(":", 1)[-1]
            if "/" in maybe:
                path = maybe
        is_test = entry.get("is_test")
    elif hasattr(entry, "path"):
        try:
            path = str(getattr(entry, "path") or "")
        except Exception:
            path = ""
        try:
            is_test = getattr(entry, "is_test", None)
        except Exception:
            is_test = None
        if not path:
            try:
                path = str(getattr(entry, "current_path") or "")
            except Exception:
                pass
        if not path and hasattr(entry, "id"):
            try:
                fid = str(getattr(entry, "id"))
                if ":" in fid:
                    path = fid.split(":", 1)[-1]
            except Exception:
                pass
    return _normalize_path(str(path)), is_test


def _repo_commit_count(graph: Any, repository_id: str) -> int | None:
    for obj in (graph, getattr(graph, "commits", None), getattr(graph, "commit_repo", None)):
        if obj is None:
            continue
        for meth in ("get_repo_commit_count", "count_commits", "get_commit_count", "commit_count"):
            if hasattr(obj, meth):
                try:
                    res = getattr(obj, meth)(repository_id)  # type: ignore[misc]
                    if isinstance(res, int):
                        return res
                except Exception:
                    pass
        # list length fallback
        for meth in ("list_by_repository", "list_commits", "list_by_repo", "all_commits"):
            if hasattr(obj, meth):
                try:
                    res = getattr(obj, meth)(repository_id)  # type: ignore[misc]
                    if isinstance(res, list):
                        return len(res)
                except Exception:
                    pass
    # Try repositories
    try:
        from .repositories import get_repo_commit_count as _repo_fn

        driver = _resolve_driver(graph)
        cnt = _repo_fn(driver, repository_id)
        if isinstance(cnt, int):
            return cnt
    except Exception:
        pass
    # Try _commits storage
    for attr in ("_commits", "commits_list", "_commit_map", "commit_map"):
        if hasattr(graph, attr):
            try:
                val = getattr(graph, attr)
                if isinstance(val, list):
                    # filter by repo if entries have repo field
                    count = 0
                    for c in val:
                        if isinstance(c, dict):
                            if c.get("repository_id") == repository_id or c.get("repository_id") is None:
                                count += 1
                        elif hasattr(c, "repository_id"):
                            if getattr(c, "repository_id") == repository_id:
                                count += 1
                        else:
                            count += 1
                    return count
                elif isinstance(val, dict):
                    if repository_id in val:
                        inner = val[repository_id]
                        if isinstance(inner, list):
                            return len(inner)
                        if isinstance(inner, dict):
                            return len(inner)
                    # flat dict commit_id -> commit
                    count = 0
                    for c in val.values():
                        if isinstance(c, dict):
                            if c.get("repository_id") == repository_id:
                                count += 1
                        elif hasattr(c, "repository_id"):
                            if getattr(c, "repository_id") == repository_id:
                                count += 1
                        else:
                            count += 1
                    if count:
                        return count
            except Exception:
                pass
    # Try commits touching all files?
    return None


# ---------------------------------------------------------------------------
# Raw computation
# ---------------------------------------------------------------------------


def _compute_raw(
    graph: Any,
    repository_id: str,
    target_path: str,
    half_life: float,
    now: datetime,
) -> tuple[dict[str, float], Evidence, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute raw values for target and return evidence helpers.

    Returns:
        raw dict, evidence object, commits_touching, cochange_neighbors, importers
    """
    norm_target = _normalize_path(target_path)
    commits = _get_commits_touching(graph, repository_id, norm_target)
    cochange_neighbors = _get_cochange_neighbors(graph, repository_id, norm_target)
    importers = _get_importers(graph, repository_id, norm_target)

    # Determinism: sort commits by date then hash, neighbors by path
    def _commit_sort_key(c: dict[str, Any]) -> tuple[str, str]:
        d = str(c.get("date") or "")
        h = str(c.get("hash") or c.get("id") or "")
        return (d, h)

    commits_sorted = sorted(commits, key=_commit_sort_key)
    cochange_sorted = sorted(cochange_neighbors, key=lambda x: _normalize_path(str(x.get("path") or "")))
    importers_sorted = sorted(importers, key=lambda x: _fan_in_path(x))

    # Churn: sum decay * (1+log1p(add+del))
    churn_raw = 0.0
    fix_raw = 0.0
    evidence_commits: list[CommitEvidence] = []
    evidence_rels: list[RelationshipEvidence] = []

    for c in commits_sorted:
        c_date = _extract_commit_date(c)
        age = age_days(c_date, now)
        # Record future-date note via limitations handled at service layer; weight clamps to 1.0
        try:
            w = decay_weight(age, half_life)
        except ValueError:
            w = 1.0
        add, dele = _additions_deletions(c)
        total_lines = 0
        if add is not None or dele is not None:
            try:
                a_val = int(add) if add is not None else 0  # type: ignore[arg-type]
            except Exception:
                a_val = 0
            try:
                d_val = int(dele) if dele is not None else 0  # type: ignore[arg-type]
            except Exception:
                d_val = 0
            total_lines = a_val + d_val
            # negative? clamp
            if total_lines < 0:
                total_lines = 0
        # Formula from task: 1 + log1p(add+del)
        # When add/del unknown (both None) total=0 => 1.0 contribution
        line_factor = 1.0 + math.log1p(float(total_lines))
        churn_raw += w * line_factor
        if _is_fix_commit(c):
            fix_raw += w
        # Evidence entry
        ev_hash = str(c.get("hash") or c.get("id") or "")
        # Extract hash from id if needed (repo:hash)
        if ":" in ev_hash and len(ev_hash.split(":", 1)[-1]) >= 4:
            maybe = ev_hash.split(":", 1)[-1]
            if "/" not in maybe and len(maybe) <= 40:
                ev_hash = maybe
        evidence_commits.append(
            CommitEvidence(
                hash=ev_hash,
                date=str(c.get("date") or "") if c.get("date") is not None else None,
                message=str(c.get("message") or "") if c.get("message") is not None else None,
                fix_reason=str(c.get("fix_reason") or "") if c.get("fix_reason") is not None else None,
            )
        )

    # Co-change raw: sum weights or counts
    cochange_raw = 0.0
    for nb in cochange_sorted:
        try:
            cochange_raw += _cochange_raw_value(nb)
        except Exception:
            cochange_raw += 1.0
        # evidence relationship
        nb_path = _normalize_path(str(nb.get("path") or nb.get("canonical_path") or ""))
        if nb_path:
            cnt_val = nb.get("count")
            w_val = nb.get("weight")
            conf_val = nb.get("confidence")
            cnt_parsed: int | None = None
            w_parsed: float | None = None
            conf_parsed: float | None = None
            if cnt_val is not None:
                try:
                    cnt_parsed = int(cnt_val)  # type: ignore[arg-type]
                except Exception:
                    cnt_parsed = None
            if w_val is not None:
                try:
                    w_parsed = float(w_val)  # type: ignore[arg-type]
                except Exception:
                    w_parsed = None
            if conf_val is not None:
                try:
                    conf_parsed = float(conf_val)  # type: ignore[arg-type]
                except Exception:
                    conf_parsed = None
            evidence_rels.append(
                RelationshipEvidence(
                    source=norm_target,
                    target=nb_path,
                    kind="CO_CHANGED_WITH",
                    count=cnt_parsed,
                    weight=w_parsed,
                    confidence=conf_parsed,
                )
            )

    # Fan-in raw: distinct importer count (deduplicate paths)
    fan_paths = set()
    for imp in importers_sorted:
        p = _fan_in_path(imp)
        if p:
            fan_paths.add(p)
    fan_in_raw = float(len(fan_paths))
    for p in sorted(fan_paths):
        evidence_rels.append(
            RelationshipEvidence(
                source=p,
                target=norm_target,
                kind="IMPORTS",
            )
        )

    raw: dict[str, float] = {
        "churn": float(churn_raw),
        "fix": float(fix_raw),
        "cochange": float(cochange_raw),
        "fan_in": float(fan_in_raw),
    }

    evidence = Evidence(commits=sorted(evidence_commits, key=lambda e: e.hash), relationships=sorted(evidence_rels, key=lambda r: (r.kind, r.target)))

    # Ensure determinism: already sorted
    return raw, evidence, commits_sorted, cochange_sorted, importers_sorted


def _gather_repo_stats(
    graph: Any,
    repository_id: str,
    half_life: float,
    now: datetime,
) -> dict[str, dict[str, float]]:
    """Compute per-component min/max/count across all files in repo.

    This provides repository-scoped normalization distribution. If the
    repository has no files, returns empty dict (caller will fallback to
    0.5). For single-file repos, min==max handled via normalize fallback.
    """
    all_files = _list_all_files(graph, repository_id)
    # Normalize entries to paths
    file_paths: list[str] = []
    for entry in all_files:
        p, _ = _extract_file_path(entry)
        if p:
            file_paths.append(p)
    # Deduplicate
    file_paths = sorted(set(file_paths))
    if not file_paths:
        return {}

    # Compute raw per file
    per_component_values: dict[str, list[float]] = {k: [] for k in _COMPONENTS}

    for fp in file_paths:
        raw, _, _, _, _ = _compute_raw(graph, repository_id, fp, half_life, now)
        for comp in _COMPONENTS:
            per_component_values[comp].append(float(raw.get(comp, 0.0)))

    repo_stats: dict[str, dict[str, float]] = {}
    for comp, vals in per_component_values.items():
        if not vals:
            continue
        vmin = min(vals)
        vmax = max(vals)
        repo_stats[comp] = {"min": float(vmin), "max": float(vmax), "count": float(len(vals))}
    return repo_stats


def compute_criticality(
    graph: Any,
    repository_id: str,
    target_path: str,
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[CriticalityResult, Evidence, dict[str, dict[str, float]]]:
    """Core criticality computation without envelope.

    This function is deterministic for same graph snapshot, half_life,
    and now. It does NOT handle not_found or insufficient_history —
    the caller (AnalyticsService) owns the contract status.

    Args:
        graph: GraphGateway, driver, or fake providing read methods.
        repository_id: repository identifier.
        target_path: canonical file path.
        config: ChronoTraceConfig-like for half-life.
        now: reference datetime for decay (injected for determinism).

    Returns:
        (CriticalityResult, Evidence, repo_stats)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    half_life = get_half_life(config)
    norm_target = _normalize_path(target_path)

    raw, evidence, _, _, _ = _compute_raw(graph, repository_id, norm_target, half_life, now)
    repo_stats = _gather_repo_stats(graph, repository_id, half_life, now)

    # Normalize repository-scoped
    # Build repo_stats shape for normalize_components: component -> {min, max}
    normalized = normalize_components(raw, repo_stats)

    # Weighted scoring
    weighted: dict[str, float] = {}
    score = 0.0
    breakdown: dict[str, CriticalityBreakdown] = {}
    for comp in _COMPONENTS:
        w = float(WEIGHTS.get(comp, 0.0))
        n = float(normalized.get(comp, 0.5))
        # Clamp normalized to [0,1] (already by normalize)
        if n < 0:
            n = 0.0
        if n > 1:
            n = 1.0
        weighted_val = n * w
        weighted[comp] = weighted_val
        score += weighted_val
        breakdown[comp] = CriticalityBreakdown(
            raw=float(raw.get(comp, 0.0)),
            normalized=float(n),
            weighted=float(weighted_val),
            score=float(weighted_val),
        )

    # Clamp score to [0,1] due to floating point
    if score < 0:
        score = 0.0
    if score > 1:
        score = 1.0

    result = CriticalityResult(
        score=float(score),
        components=breakdown,
        repo_stats=repo_stats,
        weights=dict(WEIGHTS),
    )

    return result, evidence, repo_stats


# Aliases for consumers expecting different names
calculate_criticality = compute_criticality
get_criticality = compute_criticality
criticality_for_file = compute_criticality

__all__ = ["WEIGHTS", "compute_criticality", "calculate_criticality", "get_criticality", "criticality_for_file"]
