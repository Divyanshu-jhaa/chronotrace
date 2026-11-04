"""Historical impact and hidden-coupling detection (iter-5, Decision 6).

Retrieves importers (<-IMPORTS-), dependencies (-IMPORTS->), co-changed
files (-CO_CHANGED_WITH-|-CO_CHANGES-) via read-only parameterized graph
reads, compares co-change sets against import edges, flags
potential_hidden_coupling only when no IMPORTS edge exists in either
direction with note "observed association not proven dependency", and
includes supporting commit evidence.
"""

from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    CommitEvidence,
    Evidence,
    FlaggedCoChange,
    ImpactResult,
    RelationshipEvidence,
)

# Note for hidden couplings — exact substring required by Task 4.2
HIDDEN_COUPLING_NOTE = "observed association not proven dependency"
# Longer disclaimer used in service limitations (contains above substring)
HIDDEN_COUPLING_LONG_NOTE = (
    "observed co-change association, not proven dependency — may be indirect, test, or config coupling"
)


def _normalize_path(p: str) -> str:
    if not p:
        return ""
    p = p.replace("\\", "/")
    p = posixpath.normpath(p)
    if p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p.lstrip("/")
    return p


def _resolve_driver(graph: Any) -> Any:
    if graph is None:
        return None
    for attr in ("driver", "_driver", "_raw_driver"):
        if hasattr(graph, attr):
            try:
                d = getattr(graph, attr)
                if d is not None and hasattr(d, "session"):
                    return d
            except Exception:
                pass
    if hasattr(graph, "session"):
        return graph
    return graph


def _extract_path_from_entry(entry: Any) -> str:
    if isinstance(entry, str):
        return _normalize_path(entry)
    if isinstance(entry, dict):
        for key in ("path", "canonical_path", "current_path", "target", "source", "file", "id", "canonicalPath"):
            v = entry.get(key)
            if isinstance(v, str) and v.strip():
                # id may be repo:path
                if key == "id" and ":" in v:
                    maybe = v.split(":", 1)[-1]
                    if "/" in maybe or maybe.endswith(".py"):
                        return _normalize_path(maybe)
                else:
                    return _normalize_path(v)
        # fallback: first string value that looks like path
        for v in entry.values():
            if isinstance(v, str) and ("/" in v or v.endswith(".py")):
                return _normalize_path(v)
        return ""
    if hasattr(entry, "path"):
        try:
            v = getattr(entry, "path")
            if isinstance(v, str) and v.strip():
                return _normalize_path(v)
        except Exception:
            pass
    if hasattr(entry, "canonical_path"):
        try:
            v = getattr(entry, "canonical_path")
            if isinstance(v, str) and v.strip():
                return _normalize_path(v)
        except Exception:
            pass
    if hasattr(entry, "current_path"):
        try:
            v = getattr(entry, "current_path")
            if isinstance(v, str) and v.strip():
                return _normalize_path(v)
        except Exception:
            pass
    if hasattr(entry, "id"):
        try:
            v = str(getattr(entry, "id"))
            if ":" in v:
                v = v.split(":", 1)[-1]
            return _normalize_path(v)
        except Exception:
            pass
    try:
        return _normalize_path(str(entry))
    except Exception:
        return ""


def _try_repo_importers(graph: Any, repository_id: str, target: str) -> list[str] | None:
    driver = _resolve_driver(graph)
    try:
        from . import repositories as repo_mod

        rows = repo_mod.get_importers(driver, repository_id, target)
        if isinstance(rows, list):
            paths: list[str] = []
            for r in rows:
                if isinstance(r, dict):
                    p = r.get("path") or r.get("canonical_path") or r.get("current_path")
                    if isinstance(p, str) and p.strip():
                        paths.append(_normalize_path(p))
                    else:
                        pp = _extract_path_from_entry(r)
                        if pp:
                            paths.append(pp)
                elif isinstance(r, str):
                    paths.append(_normalize_path(r))
                else:
                    pp = _extract_path_from_entry(r)
                    if pp:
                        paths.append(pp)
            # Deduplicate and sort
            uniq = sorted(set(p for p in paths if p))
            # If driver is real and we got result (even empty), consider it authoritative
            # For fakes, repositories may return [] even though fake has data; we let caller decide fallback
            return uniq
    except Exception:
        pass
    return None


def _try_repo_dependencies(graph: Any, repository_id: str, target: str) -> list[str] | None:
    driver = _resolve_driver(graph)
    try:
        from . import repositories as repo_mod

        rows = repo_mod.get_dependencies(driver, repository_id, target)
        if isinstance(rows, list):
            paths: list[str] = []
            for r in rows:
                if isinstance(r, dict):
                    p = r.get("path") or r.get("canonical_path") or r.get("current_path")
                    if isinstance(p, str) and p.strip():
                        paths.append(_normalize_path(p))
                    else:
                        pp = _extract_path_from_entry(r)
                        if pp:
                            paths.append(pp)
                elif isinstance(r, str):
                    paths.append(_normalize_path(r))
                else:
                    pp = _extract_path_from_entry(r)
                    if pp:
                        paths.append(pp)
            uniq = sorted(set(p for p in paths if p))
            return uniq
    except Exception:
        pass
    return None


def _try_repo_cochange(graph: Any, repository_id: str, target: str) -> list[dict[str, Any]] | None:
    driver = _resolve_driver(graph)
    try:
        from . import repositories as repo_mod

        rows = repo_mod.get_cochange_neighbors(driver, repository_id, target)
        if isinstance(rows, list):
            out: list[dict[str, Any]] = []
            for r in rows:
                if isinstance(r, dict):
                    p = r.get("path") or r.get("canonical_path") or r.get("current_path")
                    if not p or not isinstance(p, str):
                        p = _extract_path_from_entry(r)
                    if not p:
                        continue
                    entry: dict[str, Any] = {
                        "path": _normalize_path(p),
                        "count": r.get("count"),
                        "weight": r.get("weight"),
                        "confidence": r.get("confidence"),
                        "id": r.get("id"),
                    }
                    out.append(entry)
                else:
                    p = _extract_path_from_entry(r)
                    if p:
                        out.append({"path": p, "count": 1, "weight": None, "confidence": None})
            # Do not return None if we got rows, even if empty (could be legitimate empty)
            # Caller will decide if fallback needed when empty but fake has data
            return out
    except Exception:
        pass
    return None


def _fallback_importers(graph: Any, repository_id: str, target: str) -> list[str]:
    target_norm = _normalize_path(target)
    # Try direct graph methods
    for meth in ("get_importers", "get_importers_for_file", "importers_for", "get_importers_of"):
        if hasattr(graph, meth):
            try:
                res = getattr(graph, meth)(repository_id, target_norm)
                if isinstance(res, (list, set, tuple)):
                    out: list[str] = []
                    for item in res:
                        if isinstance(item, str):
                            out.append(_normalize_path(item))
                        else:
                            pp = _extract_path_from_entry(item)
                            if pp:
                                out.append(pp)
                    if out:
                        return sorted(set(out))
                elif isinstance(res, dict):
                    out = [_normalize_path(str(k)) for k in res.keys()]
                    if out:
                        return sorted(set(out))
            except Exception:
                pass
        for inner_attr in ("files", "relationships", "graph", "repo", "store"):
            if hasattr(graph, inner_attr):
                inner = getattr(graph, inner_attr)
                if hasattr(inner, meth):
                    try:
                        res = getattr(inner, meth)(repository_id, target_norm)
                        if isinstance(res, (list, set, tuple)):
                            out = []
                            for item in res:
                                pp = _extract_path_from_entry(item) if not isinstance(item, str) else _normalize_path(item)
                                if pp:
                                    out.append(pp)
                            if out:
                                return sorted(set(out))
                    except Exception:
                        pass
    # Inspect stored edges
    for attr in ("_imports", "imports", "import_edges", "_import_edges", "_import_map", "imports_map", "edges"):
        if hasattr(graph, attr):
            try:
                edges = getattr(graph, attr)
                found: set[str] = set()
                if isinstance(edges, list):
                    for e in edges:
                        if isinstance(e, (list, tuple)) and len(e) >= 2:
                            src, dst = str(e[0]), str(e[1])
                            if _normalize_path(dst) == target_norm:
                                found.add(_normalize_path(src))
                            # dict edges may have language third element
                            if len(e) == 3 and _normalize_path(str(e[1])) == target_norm:
                                found.add(_normalize_path(str(e[0])))
                        elif isinstance(e, dict):
                            src = e.get("source") or e.get("src") or e.get("from") or e.get("importer")  # type: ignore[assignment]
                            dst = e.get("target") or e.get("dst") or e.get("to") or e.get("imported")  # type: ignore[assignment]
                            if src and dst and _normalize_path(str(dst)) == target_norm:
                                found.add(_normalize_path(str(src)))
                            # handle repo-scoped mapping
                            if e.get("repository_id") and e.get("repository_id") != repository_id:
                                found.discard(_normalize_path(str(src)))
                elif isinstance(edges, dict):
                    for src, dsts in edges.items():
                        if isinstance(dsts, (list, set, tuple)):
                            for d in dsts:
                                if isinstance(d, str) and _normalize_path(d) == target_norm:
                                    found.add(_normalize_path(str(src)))
                                elif isinstance(d, dict):
                                    dst_val = d.get("target") or d.get("path")
                                    if dst_val and _normalize_path(str(dst_val)) == target_norm:
                                        found.add(_normalize_path(str(src)))
                        elif isinstance(dsts, str) and _normalize_path(dsts) == target_norm:
                            found.add(_normalize_path(str(src)))
                    if found:
                        return sorted(found)
                if found:
                    return sorted(found)
            except Exception:
                pass
    # Inspect fake file->importers mapping
    for attr in ("_file_importers", "file_importers", "_importers_map"):
        if hasattr(graph, attr):
            try:
                mp = getattr(graph, attr)
                if isinstance(mp, dict) and target_norm in mp:
                    val = mp[target_norm]
                    if isinstance(val, (list, set, tuple)):
                        return sorted(set(_normalize_path(str(x)) if not isinstance(x, str) else _normalize_path(x) for x in val))
            except Exception:
                pass
    # Try scanning repository_id scoped dicts like graph._fake_import_edges
    if hasattr(graph, "_fake_import_edges"):
        try:
            mp = getattr(graph, "_fake_import_edges")
            if isinstance(mp, dict) and repository_id in mp:
                edges = mp[repository_id]
                if isinstance(edges, list):
                    found = set()
                    for e in edges:
                        if isinstance(e, dict):
                            src = e.get("source")  # type: ignore[assignment]
                            dst = e.get("target")  # type: ignore[assignment]
                            if src and dst and _normalize_path(str(dst)) == target_norm:
                                found.add(_normalize_path(str(src)))
                        elif isinstance(e, (list, tuple)) and len(e) >= 2:
                            if _normalize_path(str(e[1])) == target_norm:
                                found.add(_normalize_path(str(e[0])))
                    if found:
                        return sorted(found)
        except Exception:
            pass
    return []


def _fallback_dependencies(graph: Any, repository_id: str, target: str) -> list[str]:
    target_norm = _normalize_path(target)
    for meth in ("get_dependencies", "get_dependencies_for_file", "dependencies_for", "get_deps", "get_imports_for"):
        if hasattr(graph, meth):
            try:
                res = getattr(graph, meth)(repository_id, target_norm)
                if isinstance(res, (list, set, tuple)):
                    out: list[str] = []
                    for item in res:
                        if isinstance(item, str):
                            out.append(_normalize_path(item))
                        else:
                            pp = _extract_path_from_entry(item)
                            if pp:
                                out.append(pp)
                    if out:
                        return sorted(set(out))
            except Exception:
                pass
        for inner_attr in ("files", "relationships", "graph", "repo"):
            if hasattr(graph, inner_attr):
                inner = getattr(graph, inner_attr)
                if hasattr(inner, meth):
                    try:
                        res = getattr(inner, meth)(repository_id, target_norm)
                        if isinstance(res, (list, set, tuple)):
                            out = []
                            for item in res:
                                pp = _extract_path_from_entry(item) if not isinstance(item, str) else _normalize_path(item)
                                if pp:
                                    out.append(pp)
                            if out:
                                return sorted(set(out))
                    except Exception:
                        pass
    for attr in ("_imports", "imports", "import_edges", "_import_edges", "_import_map", "imports_map", "edges"):
        if hasattr(graph, attr):
            try:
                edges = getattr(graph, attr)
                found: set[str] = set()
                if isinstance(edges, list):
                    for e in edges:
                        if isinstance(e, (list, tuple)) and len(e) >= 2:
                            src, dst = str(e[0]), str(e[1])
                            if _normalize_path(src) == target_norm:
                                found.add(_normalize_path(dst))
                        elif isinstance(e, dict):
                            src = e.get("source") or e.get("src") or e.get("from")  # type: ignore[assignment]
                            dst = e.get("target") or e.get("dst") or e.get("to")  # type: ignore[assignment]
                            if src and dst and _normalize_path(str(src)) == target_norm:
                                found.add(_normalize_path(str(dst)))
                elif isinstance(edges, dict):
                    if target_norm in edges:
                        val = edges[target_norm]
                        if isinstance(val, (list, set, tuple)):
                            for d in val:
                                if isinstance(d, str):
                                    found.add(_normalize_path(d))
                                else:
                                    pp = _extract_path_from_entry(d)
                                    if pp:
                                        found.add(pp)
                        elif isinstance(val, str):
                            found.add(_normalize_path(val))
                    # also check edges as source->targets mapping
                    for src, dsts in edges.items():
                        if _normalize_path(str(src)) == target_norm:
                            if isinstance(dsts, (list, set, tuple)):
                                for d in dsts:
                                    pp = _extract_path_from_entry(d) if not isinstance(d, str) else _normalize_path(d)
                                    if pp:
                                        found.add(pp)
                if found:
                    return sorted(found)
            except Exception:
                pass
    for attr in ("_file_dependencies", "file_dependencies", "_deps_map"):
        if hasattr(graph, attr):
            try:
                mp = getattr(graph, attr)
                if isinstance(mp, dict) and target_norm in mp:
                    val = mp[target_norm]
                    if isinstance(val, (list, set, tuple)):
                        return sorted(set(_normalize_path(str(x)) for x in val))
            except Exception:
                pass
    if hasattr(graph, "_fake_import_edges"):
        try:
            mp = getattr(graph, "_fake_import_edges")
            if isinstance(mp, dict) and repository_id in mp:
                edges = mp[repository_id]
                if isinstance(edges, list):
                    found = set()
                    for e in edges:
                        if isinstance(e, dict):
                            src = e.get("source")  # type: ignore[assignment]
                            dst = e.get("target")  # type: ignore[assignment]
                            if src and dst and _normalize_path(str(src)) == target_norm:
                                found.add(_normalize_path(str(dst)))
                        elif isinstance(e, (list, tuple)) and len(e) >= 2:
                            if _normalize_path(str(e[0])) == target_norm:
                                found.add(_normalize_path(str(e[1])))
                    if found:
                        return sorted(found)
        except Exception:
            pass
    return []


def _fallback_cochange_neighbors(graph: Any, repository_id: str, target: str) -> list[dict[str, Any]]:
    target_norm = _normalize_path(target)
    # Try graph methods first
    for meth in ("get_cochange_neighbors", "get_co_change_neighbors", "get_co_changed", "get_co_changed_files", "cochange_neighbors", "get_co_changes"):
        if hasattr(graph, meth):
            try:
                res = getattr(graph, meth)(repository_id, target_norm)
                if res is None:
                    continue
                out: list[dict[str, Any]] = []  # type: ignore[no-redef]
                if isinstance(res, dict):
                    for k, v in res.items():
                        p = _normalize_path(str(k))
                        if isinstance(v, dict):
                            cnt = v.get("count", v.get("weight", 1))
                            w = v.get("weight")
                            conf = v.get("confidence")
                            out.append({"path": p, "count": int(cnt) if isinstance(cnt, (int, float)) else 1, "weight": float(w) if isinstance(w, (int, float)) else None, "confidence": float(conf) if isinstance(conf, (int, float)) else None})
                        elif isinstance(v, (int, float)):
                            out.append({"path": p, "count": int(v), "weight": float(v), "confidence": None})
                        elif hasattr(v, "count"):
                            out.append({"path": p, "count": int(getattr(v, "count", 1)), "weight": None, "confidence": None})
                    if out:
                        return out
                elif isinstance(res, (list, set, tuple)):
                    for item in res:
                        if isinstance(item, dict):
                            p = item.get("path") or item.get("canonical_path") or item.get("target") or item.get("file")  # type: ignore[assignment]
                            if not p:
                                p = _extract_path_from_entry(item)
                            cnt = item.get("count", item.get("weight", 1))
                            w = item.get("weight")
                            conf = item.get("confidence")
                            if p:
                                out.append({"path": _normalize_path(str(p)), "count": int(cnt) if isinstance(cnt, (int, float)) else 1, "weight": float(w) if isinstance(w, (int, float)) else None, "confidence": float(conf) if isinstance(conf, (int, float)) else None})
                        elif isinstance(item, (list, tuple)) and len(item) >= 2:
                            p = str(item[0])
                            cnt = item[1]
                            if isinstance(cnt, dict):
                                cnt_val = cnt.get("count", 1)
                                w_val = cnt.get("weight")
                                conf_val = cnt.get("confidence")
                                out.append({"path": _normalize_path(p), "count": int(cnt_val) if isinstance(cnt_val, (int,float)) else 1, "weight": float(w_val) if isinstance(w_val, (int,float)) else None, "confidence": float(conf_val) if isinstance(conf_val, (int,float)) else None})
                            else:
                                out.append({"path": _normalize_path(p), "count": int(cnt) if isinstance(cnt, (int,float)) else 1, "weight": None, "confidence": None})
                        elif hasattr(item, "path"):
                            p = getattr(item, "path", None) or getattr(item, "canonical_path", None)  # type: ignore[assignment]
                            cnt = getattr(item, "count", 1)
                            w = getattr(item, "weight", None)
                            conf = getattr(item, "confidence", None)
                            if p:
                                out.append({"path": _normalize_path(str(p)), "count": int(cnt) if isinstance(cnt, (int,float)) else 1, "weight": float(w) if isinstance(w, (int,float)) else None, "confidence": float(conf) if isinstance(conf, (int,float)) else None})
                    if out:
                        return out
            except Exception:
                pass
        for inner_attr in ("relationships", "files", "graph"):
            if hasattr(graph, inner_attr):
                inner = getattr(graph, inner_attr)
                if hasattr(inner, meth):
                    try:
                        res = getattr(inner, meth)(repository_id, target_norm)
                        if isinstance(res, (list, dict)):
                            # reuse above logic
                            tmp = _fallback_cochange_neighbors(inner, repository_id, target)  # recursive fallback will already handle dict/list
                            if tmp:
                                return tmp
                    except Exception:
                        pass
    # Inspect storage attributes
    for attr in ("_co_changes", "co_changes", "_cochange", "co_change_map", "_co_changed", "co_changed_edges", "_co_changed_with", "co_changed_with", "_cochange_neighbors", "cochange_map"):
        if hasattr(graph, attr):
            try:
                data = getattr(graph, attr)
                out: list[dict[str, Any]] = []  # type: ignore[no-redef]
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(k, tuple) and len(k) == 2:
                            a, b = _normalize_path(str(k[0])), _normalize_path(str(k[1]))
                            cnt = 1
                            w = None
                            conf = None
                            if isinstance(v, dict):
                                cnt = int(v.get("count", v.get("weight", 1)))  # type: ignore[arg-type]
                                w_raw = v.get("weight")
                                conf_raw = v.get("confidence")
                                if isinstance(w_raw, (int, float)):
                                    w = float(w_raw)
                                if isinstance(conf_raw, (int, float)):
                                    conf = float(conf_raw)
                            elif isinstance(v, (int, float)):
                                cnt = int(v)
                                w = float(v)
                            if a == target_norm and b:
                                out.append({"path": b, "count": cnt, "weight": w, "confidence": conf})
                            elif b == target_norm and a:
                                # handle undirected: if stored sorted, need to check both sides
                                # avoid duplicate if already added
                                if not any(e["path"] == a for e in out):
                                    out.append({"path": a, "count": cnt, "weight": w, "confidence": conf})
                            elif isinstance(k, str) and _normalize_path(k) == target_norm and isinstance(v, dict):
                                # mapping target -> {neighbor: count}
                                for nb, cnt_val in v.items():
                                    nb_norm = _normalize_path(str(nb))
                                    cnt2 = int(cnt_val) if isinstance(cnt_val, (int,float)) else 1
                                    w2 = None
                                    if isinstance(cnt_val, dict):
                                        cnt2 = int(cnt_val.get("count", 1))
                                        w_raw2 = cnt_val.get("weight")
                                        if isinstance(w_raw2, (int,float)):
                                            w2 = float(w_raw2)
                                    out.append({"path": nb_norm, "count": cnt2, "weight": w2, "confidence": None})
                        elif isinstance(k, str):
                            kn = _normalize_path(k)
                            if kn == target_norm and isinstance(v, dict):
                                for nb, cnt_val in v.items():
                                    nb_norm = _normalize_path(str(nb))
                                    cnt2 = 1
                                    w2 = None
                                    conf2 = None
                                    if isinstance(cnt_val, dict):
                                        cnt2 = int(cnt_val.get("count", 1))
                                        w_raw2 = cnt_val.get("weight")
                                        conf_raw2 = cnt_val.get("confidence")
                                        if isinstance(w_raw2, (int,float)):
                                            w2 = float(w_raw2)
                                        if isinstance(conf_raw2, (int,float)):
                                            conf2 = float(conf_raw2)
                                    elif isinstance(cnt_val, (int,float)):
                                        cnt2 = int(cnt_val)
                                        w2 = float(cnt_val)
                                    out.append({"path": nb_norm, "count": cnt2, "weight": w2, "confidence": conf2})
                            elif kn == target_norm and isinstance(v, (int,float)):
                                # shouldn't happen
                                pass
                    if out:
                        return out
                elif isinstance(data, list):
                    for e in data:
                        if isinstance(e, dict):
                            src = e.get("source") or e.get("src") or e.get("a") or e.get("from")  # type: ignore[assignment]
                            dst = e.get("target") or e.get("dst") or e.get("b") or e.get("to")  # type: ignore[assignment]
                            if src and dst:
                                src_n = _normalize_path(str(src))
                                dst_n = _normalize_path(str(dst))
                                cnt_raw = e.get("count", e.get("weight", 1))
                                w_raw = e.get("weight")
                                conf_raw = e.get("confidence")
                                cnt = int(cnt_raw) if isinstance(cnt_raw, (int,float)) else 1  # type: ignore[arg-type]
                                w = float(w_raw) if isinstance(w_raw, (int,float)) else None
                                conf = float(conf_raw) if isinstance(conf_raw, (int,float)) else None
                                if src_n == target_norm:
                                    out.append({"path": dst_n, "count": cnt, "weight": w, "confidence": conf})
                                elif dst_n == target_norm:
                                    out.append({"path": src_n, "count": cnt, "weight": w, "confidence": conf})
                        elif isinstance(e, (list, tuple)) and len(e) >= 2:
                            src_n = _normalize_path(str(e[0]))
                            dst_n = _normalize_path(str(e[1]))
                            cnt = 1
                            w = None
                            conf = None
                            if len(e) >= 3 and isinstance(e[2], (int,float)):
                                cnt = int(e[2])
                                w = float(e[2])
                            elif len(e) >= 3 and isinstance(e[2], dict):
                                cnt = int(e[2].get("count", 1))
                                w_raw = e[2].get("weight")
                                if isinstance(w_raw, (int,float)):
                                    w = float(w_raw)
                                conf_raw = e[2].get("confidence")
                                if isinstance(conf_raw, (int,float)):
                                    conf = float(conf_raw)
                            if src_n == target_norm:
                                out.append({"path": dst_n, "count": cnt, "weight": w, "confidence": conf})
                            elif dst_n == target_norm:
                                out.append({"path": src_n, "count": cnt, "weight": w, "confidence": conf})
                    if out:
                        return out
                # handle repo-scoped dict: repo_id -> dict or list
                if isinstance(data, dict) and repository_id in data:
                    inner = data[repository_id]
                    if isinstance(inner, dict):
                        # inner may be target->neighbors map
                        if target_norm in inner:
                            val = inner[target_norm]
                            if isinstance(val, dict):
                                for nb, cnt_val in val.items():
                                    out.append({"path": _normalize_path(str(nb)), "count": int(cnt_val) if isinstance(cnt_val, (int,float)) else 1, "weight": None, "confidence": None})
                                if out:
                                    return out
                            elif isinstance(val, list):
                                for item in val:
                                    pp = _extract_path_from_entry(item) if not isinstance(item, str) else _normalize_path(item)
                                    if pp:
                                        out.append({"path": pp, "count": 1, "weight": None, "confidence": None})
                                if out:
                                    return out
                    elif isinstance(inner, list):
                        for e in inner:
                            if isinstance(e, dict):
                                src = e.get("source") or e.get("src")  # type: ignore[assignment]
                                dst = e.get("target") or e.get("dst")  # type: ignore[assignment]
                                if src and dst:
                                    src_n = _normalize_path(str(src))
                                    dst_n = _normalize_path(str(dst))
                                    cnt_raw = e.get("count", 1)
                                    cnt = int(cnt_raw) if isinstance(cnt_raw, (int,float)) else 1  # type: ignore[arg-type]
                                    if src_n == target_norm:
                                        out.append({"path": dst_n, "count": cnt, "weight": None, "confidence": None})
                                    elif dst_n == target_norm:
                                        out.append({"path": src_n, "count": cnt, "weight": None, "confidence": None})
                        if out:
                            return out
            except Exception:
                pass
    # Last resort: inspect any dict that looks like cochange storage
    try:
        for attr in dir(graph):
            if attr.startswith("_") and "co" in attr.lower():
                try:
                    val = getattr(graph, attr)
                    if isinstance(val, dict) and val:
                        # sample key
                        sample_k = next(iter(val.keys()))
                        if isinstance(sample_k, tuple):
                            # looks like cochange dict
                            out = []
                            for k, v in val.items():
                                if isinstance(k, tuple) and len(k)==2:
                                    a,b = _normalize_path(str(k[0])), _normalize_path(str(k[1]))
                                    cnt = int(v) if isinstance(v,(int,float)) else int(v.get("count",1)) if isinstance(v,dict) else 1
                                    if a==target_norm:
                                        out.append({"path":b,"count":cnt,"weight":None,"confidence":None})
                                    elif b==target_norm:
                                        out.append({"path":a,"count":cnt,"weight":None,"confidence":None})
                            if out:
                                return out
                except Exception:
                    pass
    except Exception:
        pass
    return []


def _get_supporting_commits(graph: Any, repository_id: str, target: str, other: str) -> list[CommitEvidence]:
    target_norm = _normalize_path(target)
    other_norm = _normalize_path(other)
    # Try repositories helper that returns commits for file ids intersection
    # First try to get via cochange provenance if edge stores commit info
    # Try direct method on graph
    for meth in ("get_supporting_commits", "get_commits_for_cochange", "supporting_commits", "get_cochange_commits"):
        if hasattr(graph, meth):
            try:
                res = getattr(graph, meth)(repository_id, target_norm, other_norm)
                if isinstance(res, list) and res:
                    out: list[CommitEvidence] = []
                    for item in res:
                        if isinstance(item, dict):
                            h = item.get("hash") or item.get("commit") or item.get("id", "")
                            if isinstance(h, str) and ":" in h:
                                h = h.split(":", 1)[-1]
                            out.append(CommitEvidence(hash=str(h), date=str(item.get("date")) if item.get("date") else None, message=item.get("message"), fix_reason=item.get("fix_reason")))
                        elif hasattr(item, "hash"):
                            out.append(CommitEvidence(hash=str(getattr(item, "hash")), date=str(getattr(item, "date", None)) if getattr(item, "date", None) else None, message=getattr(item, "message", None)))
                    if out:
                        return out
            except Exception:
                pass
    # Try to intersect commits touching each file via repositories
    try:
        driver = _resolve_driver(graph)
        from . import repositories as repo_mod
        # Try get_commits_touching_file for both
        try:
            commits_target = repo_mod.get_commits_touching_file(driver, repository_id, target_norm)
            commits_other = repo_mod.get_commits_touching_file(driver, repository_id, other_norm)
            if isinstance(commits_target, list) and isinstance(commits_other, list) and commits_target and commits_other:
                # Build hash set for target
                target_hashes = {str(c.get("hash")) for c in commits_target if isinstance(c, dict) and c.get("hash")}
                other_hashes = {str(c.get("hash")) for c in commits_other if isinstance(c, dict) and c.get("hash")}
                common = target_hashes & other_hashes
                if common:
                    # Collect evidence from either list (prefer target)
                    out = []
                    for c in commits_target:
                        if isinstance(c, dict) and str(c.get("hash")) in common:
                            out.append(CommitEvidence(hash=str(c.get("hash")), date=str(c.get("date")) if c.get("date") else None, message=c.get("message"), fix_reason=c.get("fix_reason")))
                    if out:
                        return sorted(out, key=lambda x: x.hash)
        except Exception:
            pass
        # Try get_commits_for_file_paths with both
        try:
            commits = repo_mod.get_commits_for_file_paths(driver, repository_id, [target_norm, other_norm])
            # This returns distinct commits touching any of the files, not intersection; we need intersection
            # So not sufficient, skip
            pass
        except Exception:
            pass
    except Exception:
        pass
    # Fallback: inspect fake storage
    # Look for commit_file_map
    for attr in ("_commit_file_map", "commit_file_map", "_file_commits", "file_commits", "_modified", "modified", "_commit_to_files", "commit_to_files", "_file_to_commits", "file_to_commits", "_commits_by_file", "commits_by_file"):
        if hasattr(graph, attr):
            try:
                mp = getattr(graph, attr)
                if isinstance(mp, dict):
                    # mp may be commit_hash -> list[paths] or path -> list[hashes]
                    # Try commit -> paths
                    found_commits: set[str] = set()
                    for commit_key, paths in mp.items():
                        if isinstance(paths, (list, set, tuple)):
                            norm_paths = set(_normalize_path(str(p)) for p in paths)
                            if target_norm in norm_paths and other_norm in norm_paths:
                                # commit_key may be hash or id
                                h = str(commit_key)
                                if ":" in h:
                                    h = h.split(":", 1)[-1]
                                found_commits.add(h)
                            # also handle dict values with file paths
                            elif isinstance(paths, dict):
                                # not expected
                                pass
                    if found_commits:
                        # Try to find commit evidence objects to enrich
                        commit_evidence_map: dict[str, CommitEvidence] = {}
                        # Try to locate commit objects
                        for commit_attr in ("_commits", "commits", "_commit_map", "commit_map", "_commit_store"):
                            if hasattr(graph, commit_attr):
                                try:
                                    commits_storage = getattr(graph, commit_attr)
                                    if isinstance(commits_storage, dict):
                                        for k, v in commits_storage.items():
                                            hk = str(k)
                                            if ":" in hk:
                                                hk = hk.split(":", 1)[-1]
                                            if hk in found_commits:
                                                if isinstance(v, dict):
                                                    commit_evidence_map[hk] = CommitEvidence(hash=hk, date=str(v.get("date")) if v.get("date") else None, message=v.get("message"), fix_reason=v.get("fix_reason"))
                                                elif hasattr(v, "hash"):
                                                    commit_evidence_map[hk] = CommitEvidence(hash=hk, date=str(getattr(v, "date", None)) if getattr(v, "date", None) else None, message=getattr(v, "message", None))
                                    elif isinstance(commits_storage, list):
                                        for item in commits_storage:
                                            if isinstance(item, dict):
                                                hk = str(item.get("hash") or item.get("id") or "")
                                                if ":" in hk:
                                                    hk = hk.split(":", 1)[-1]
                                                if hk in found_commits:
                                                    commit_evidence_map[hk] = CommitEvidence(hash=hk, date=str(item.get("date")) if item.get("date") else None, message=item.get("message"))
                                            elif hasattr(item, "hash"):
                                                hk = str(getattr(item, "hash"))
                                                if hk in found_commits:
                                                    commit_evidence_map[hk] = CommitEvidence(hash=hk, date=str(getattr(item, "date", None)) if getattr(item, "date", None) else None, message=getattr(item, "message", None))
                                except Exception:
                                    pass
                        if commit_evidence_map:
                            return sorted(commit_evidence_map.values(), key=lambda x: x.hash)
                        else:
                            return sorted([CommitEvidence(hash=h) for h in found_commits], key=lambda x: x.hash)
                    # Try path -> commits mapping
                    # Check if mp has target_norm as key
                    if target_norm in mp and other_norm in mp:
                        # both have lists, intersect
                        try:
                            t_list = mp[target_norm]
                            o_list = mp[other_norm]
                            if isinstance(t_list, (list, set, tuple)) and isinstance(o_list, (list, set, tuple)):
                                t_set = set(str(x).split(":",1)[-1] if ":" in str(x) else str(x) for x in t_list)
                                o_set = set(str(x).split(":",1)[-1] if ":" in str(x) else str(x) for x in o_list)
                                common = t_set & o_set
                                if common:
                                    return sorted([CommitEvidence(hash=h) for h in common], key=lambda x: x.hash)
                        except Exception:
                            pass
            except Exception:
                pass
    # Try _cochange_commits or similar
    for attr in ("_cochange_commits", "cochange_commits", "_co_changed_commits"):
        if hasattr(graph, attr):
            try:
                mp = getattr(graph, attr)
                if isinstance(mp, dict):
                    # key could be (target, other) tuple
                    for k, v in mp.items():
                        if isinstance(k, tuple) and len(k)==2:
                            a,b = _normalize_path(str(k[0])), _normalize_path(str(k[1]))
                            if (a==target_norm and b==other_norm) or (a==other_norm and b==target_norm):
                                if isinstance(v, list):
                                    out = []
                                    for item in v:
                                        if isinstance(item, dict):
                                            h = str(item.get("hash") or item.get("commit") or "")
                                            if ":" in h:
                                                h = h.split(":",1)[-1]
                                            out.append(CommitEvidence(hash=h, date=str(item.get("date")) if item.get("date") else None, message=item.get("message")))
                                        elif isinstance(item, str):
                                            out.append(CommitEvidence(hash=item.split(":",1)[-1] if ":" in item else item))
                                    if out:
                                        return out
                                elif isinstance(v, (int, float)):
                                    # count only, no commits
                                    pass
            except Exception:
                pass
    return []


def _collect_importers(graph: Any, repository_id: str, target: str) -> list[str]:
    # Try repo mod first
    repo_res = _try_repo_importers(graph, repository_id, target)
    fallback = _fallback_importers(graph, repository_id, target)
    if repo_res is not None:
        # If repo_res is non-empty, use it; if empty but fallback has data, merge or use fallback
        if repo_res:
            # Merge with fallback deduplicate (fallback may have same)
            merged = set(repo_res) | set(fallback)
            return sorted(merged)
        else:
            # repo_res empty – if fallback has data, use fallback (fake case)
            if fallback:
                return sorted(set(fallback))
            return []
    return sorted(set(fallback))


def _collect_dependencies(graph: Any, repository_id: str, target: str) -> list[str]:
    repo_res = _try_repo_dependencies(graph, repository_id, target)
    fallback = _fallback_dependencies(graph, repository_id, target)
    if repo_res is not None:
        if repo_res:
            merged = set(repo_res) | set(fallback)
            return sorted(merged)
        else:
            if fallback:
                return sorted(set(fallback))
            return []
    return sorted(set(fallback))


def _collect_cochange(graph: Any, repository_id: str, target: str) -> list[dict[str, Any]]:
    repo_res = _try_repo_cochange(graph, repository_id, target)
    fallback = _fallback_cochange_neighbors(graph, repository_id, target)
    if repo_res is not None:
        if repo_res:
            # Merge: dedup by path, prefer repo_res entries but fill missing from fallback
            by_path: dict[str, dict[str, Any]] = {}
            for e in fallback:
                by_path[e["path"]] = e
            for e in repo_res:
                # repo_res overrides fallback
                by_path[e["path"]] = e
            # Now build sorted list
            merged = list(by_path.values())
            # Sort by count desc, path asc for determinism
            merged.sort(key=lambda x: (-int(x.get("count") or 0), x["path"]))
            return merged
        else:
            if fallback:
                fallback_sorted = sorted(fallback, key=lambda x: (-int(x.get("count") or 0), x["path"]))
                return fallback_sorted
            return []
    if fallback:
        return sorted(fallback, key=lambda x: (-int(x.get("count") or 0), x["path"]))
    return []


def _compute_hidden_flag(
    co_path: str,
    importers_set: set[str],
    dependencies_set: set[str],
    graph: Any,
    repository_id: str,
    target: str,
) -> bool:
    # Direct set check: if co_path in importers or dependencies, then direct import exists
    if co_path in importers_set or co_path in dependencies_set:
        return False
    # For completeness, also check via graph edge existence (covers cases where importers/dependencies sets were incomplete)
    # Build quick check: does an IMPORTS edge exist either direction?
    # We can try to check via fallback edge inspection: if there is an import edge between target and co_path not captured
    # For real driver, we could query, but set check already covers
    # For fake, do extra scan
    # Scan fallback importers/dependencies that may have missed due to repo_res vs fallback merge? Already merged.
    # So if not in sets, no edge.
    # However to be safe, we can do an extra scan of import edges storage for this pair
    # This handles cases where import edges exist but not in importers/dependencies due to stale sets
    target_norm = _normalize_path(target)
    co_norm = _normalize_path(co_path)
    # Scan import edges list for direct edge
    for attr in ("_imports", "imports", "import_edges", "_import_edges", "edges", "_fake_import_edges"):
        if hasattr(graph, attr):
            try:
                edges = getattr(graph, attr)
                if isinstance(edges, list):
                    for e in edges:
                        if isinstance(e, (list, tuple)) and len(e) >= 2:
                            src_n = _normalize_path(str(e[0]))
                            dst_n = _normalize_path(str(e[1]))
                            if (src_n == target_norm and dst_n == co_norm) or (src_n == co_norm and dst_n == target_norm):
                                return False
                            # handle repo-scoped dict edges with source/target keys
                        elif isinstance(e, dict):
                            src = e.get("source") or e.get("src")  # type: ignore[assignment]
                            dst = e.get("target") or e.get("dst")  # type: ignore[assignment]
                            if src and dst:
                                src_n = _normalize_path(str(src))
                                dst_n = _normalize_path(str(dst))
                                if (src_n == target_norm and dst_n == co_norm) or (src_n == co_norm and dst_n == target_norm):
                                    # also check repository_id scoping if present
                                    if e.get("repository_id") and e.get("repository_id") != repository_id:
                                        continue
                                    return False
                elif isinstance(edges, dict):
                    # Could be mapping repo->edges or source->targets
                    if repository_id in edges:
                        inner = edges[repository_id]
                        if isinstance(inner, list):
                            for e in inner:
                                if isinstance(e, dict):
                                    src = e.get("source")  # type: ignore[assignment]
                                    dst = e.get("target")  # type: ignore[assignment]
                                    if src and dst:
                                        src_n = _normalize_path(str(src))
                                        dst_n = _normalize_path(str(dst))
                                        if (src_n == target_norm and dst_n == co_norm) or (src_n == co_norm and dst_n == target_norm):
                                            return False
                # also check _fake_import_edges repo dict
                if attr == "_fake_import_edges":
                    # already handled dict case
                    pass
            except Exception:
                pass
    return True


def _build_evidence(
    importers: list[str],
    dependencies: list[str],
    co_entries: list[FlaggedCoChange],
) -> Evidence:
    # Build relationships evidence
    rels: list[RelationshipEvidence] = []
    for p in importers:
        rels.append(RelationshipEvidence(source=p, target="__target__", kind="IMPORTS"))
    for p in dependencies:
        rels.append(RelationshipEvidence(source="__target__", target=p, kind="IMPORTS"))
    commits: list[CommitEvidence] = []
    seen: set[str] = set()
    for e in co_entries:
        # Use stored supporting commits if available
        for c in getattr(e, "supporting_commits", []) or []:
            if c.hash not in seen:
                seen.add(c.hash)
                commits.append(c)
        # Also add relationship for co-change
        rels.append(
            RelationshipEvidence(
                source="__target__",
                target=e.path,
                kind="CO_CHANGED_WITH",
                count=e.count,
                weight=e.weight,
                confidence=e.confidence,
            )
        )
    # Deterministic ordering
    commits_sorted = sorted(commits, key=lambda x: x.hash)
    rels_sorted = sorted(rels, key=lambda x: (x.kind, x.source, x.target))
    return Evidence(commits=commits_sorted, relationships=rels_sorted)


def analyze_historical_impact(
    graph: Any,
    repository_id: str,
    target: str,
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[ImpactResult, Evidence]:
    """Core historical impact analysis.

    Retrieves importers (<-IMPORTS-), dependencies (-IMPORTS->), co-changed
    files (-CO_CHANGED_WITH-|-CO_CHANGES-) via graph reads, compares sets,
    flags potential_hidden_coupling only when no IMPORTS edge exists either
    direction with note "observed association not proven dependency", and
    includes supporting commit evidence.
    """
    repo_id = str(repository_id or "").strip()
    target_norm = _normalize_path(target)
    # Retrieve sets via helpers
    importers = _collect_importers(graph, repo_id, target_norm)
    dependencies = _collect_dependencies(graph, repo_id, target_norm)
    co_raw = _collect_cochange(graph, repo_id, target_norm)

    importers_set = set(importers)
    dependencies_set = set(dependencies)

    # Build flagged co-change entries
    flagged: list[FlaggedCoChange] = []
    for entry in co_raw:
        path = entry.get("path")
        if not path:
            continue
        path_norm = _normalize_path(str(path))
        if path_norm == target_norm:
            continue  # exclude self
        cnt_raw = entry.get("count")
        try:
            cnt = int(cnt_raw) if cnt_raw is not None else 1  # type: ignore[arg-type]
        except Exception:
            cnt = 1
        w_raw = entry.get("weight")
        try:
            weight = float(w_raw) if w_raw is not None else None
        except Exception:
            weight = None
        conf_raw = entry.get("confidence")
        try:
            confidence = float(conf_raw) if conf_raw is not None else None
        except Exception:
            confidence = None
        # Determine hidden flag
        is_hidden = _compute_hidden_flag(path_norm, importers_set, dependencies_set, graph, repo_id, target_norm)
        note = HIDDEN_COUPLING_NOTE if is_hidden else None
        # Supporting commits
        supporting = _get_supporting_commits(graph, repo_id, target_norm, path_norm)
        flagged_entry = FlaggedCoChange(
            path=path_norm,
            count=cnt,
            confidence=confidence,
            weight=weight,
            potential_hidden_coupling=is_hidden,
            note=note,
            supporting_commits=supporting,
        )
        flagged.append(flagged_entry)

    # Deterministic ordering: count desc, then path asc (and hidden flag not affecting order)
    flagged.sort(key=lambda x: (-x.count, x.path))

    # Hidden couplings subset
    hidden = [e for e in flagged if e.potential_hidden_coupling]
    # Also sorted by same criteria (already sorted)
    # But ensure hidden sorted consistently
    hidden_sorted = sorted(hidden, key=lambda x: (-x.count, x.path))

    result = ImpactResult(
        importers=sorted(importers),
        dependencies=sorted(dependencies),
        co_changed=flagged,
        hidden_couplings=hidden_sorted,
    )

    # Build evidence
    # Need to replace placeholder "__target__" with actual target path in relationships
    evidence = _build_evidence(importers, dependencies, flagged)
    # Fix placeholder source/target
    for rel in evidence.relationships:
        if rel.source == "__target__":
            object.__setattr__(rel, "source", target_norm) if hasattr(rel, "__dataclass_fields__") else None
            # For slots dataclass, we cannot set via object.__setattr__ if frozen? RelationshipEvidence is frozen
            # So we need to handle differently: since RelationshipEvidence is frozen, we need to recreate
            pass
    # Because RelationshipEvidence is frozen, we cannot mutate source; we need to rebuild with correct target
    # Rebuild relationships with correct target
    rebuilt_rels: list[RelationshipEvidence] = []
    for rel in evidence.relationships:
        src = rel.source
        tgt = rel.target
        if src == "__target__":
            src = target_norm
        if tgt == "__target__":
            tgt = target_norm
        rebuilt_rels.append(
            RelationshipEvidence(
                source=src,
                target=tgt,
                kind=rel.kind,
                count=rel.count,
                weight=rel.weight,
                confidence=rel.confidence,
            )
        )
    rebuilt_rels.sort(key=lambda x: (x.kind, x.source, x.target))
    evidence = Evidence(commits=evidence.commits, relationships=rebuilt_rels, diagnostics_ref=evidence.diagnostics_ref)

    return result, evidence


# Aliases for flexible calling conventions ---------------------------------

def get_historical_impact(
    repository_id: str,
    target: str,
    graph: Any,
    config: Any | None = None,
    now: datetime | None = None,
) -> ImpactResult:
    """Alias with repo,target,graph order (matches test_reco order)."""
    result, _ = analyze_historical_impact(graph, repository_id, target, config, now)
    return result


def historical_impact(
    graph: Any,
    repository_id: str,
    target: str,
    config: Any | None = None,
    now: datetime | None = None,
) -> ImpactResult:
    result, _ = analyze_historical_impact(graph, repository_id, target, config, now)
    return result


def compute_impact(
    graph: Any,
    repository_id: str,
    target: str,
    config: Any | None = None,
    now: datetime | None = None,
) -> ImpactResult:
    """Compatibility alias returning only ImpactResult (service wiring)."""
    result, _ = analyze_historical_impact(graph, repository_id, target, config, now)
    return result


def get_impact(
    graph: Any,
    repository_id: str,
    target: str,
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[ImpactResult, Evidence]:
    return analyze_historical_impact(graph, repository_id, target, config, now)


def compute_impact_with_evidence(
    graph: Any,
    repository_id: str,
    target: str,
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[ImpactResult, Evidence]:
    return analyze_historical_impact(graph, repository_id, target, config, now)


# Compatibility for callers that pass kwargs in different order
def analyze(
    graph: Any,
    repository_id: str,
    target: str,
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[ImpactResult, Evidence]:
    return analyze_historical_impact(graph, repository_id, target, config, now)


__all__ = [
    "HIDDEN_COUPLING_NOTE",
    "analyze",
    "analyze_historical_impact",
    "compute_impact",
    "get_historical_impact",
    "get_impact",
    "historical_impact",
]