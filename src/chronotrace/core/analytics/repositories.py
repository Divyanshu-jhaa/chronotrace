"""Read-only graph helpers for analytics — all queries are parameterized.

Every query is scoped by repository_id and uses parameters like
$repository_id and $path. No writes are performed — only MATCH / WHERE /
RETURN / WITH / ORDER BY / and read aggregations.
"""

from __future__ import annotations

from typing import Any

try:
    from chronotrace.graph.errors import GraphValidationError as _GVE  # type: ignore[attr-defined]

    GraphValidationError = _GVE  # type: ignore[no-redef]
except Exception:  # pragma: no cover
    class GraphValidationError(Exception):  # type: ignore[no-redef]
        pass

_graph_normalize_path: Any = None
try:
    from chronotrace.graph.models import normalize_path as _tmp_normalize  # type: ignore[attr-defined]

    _graph_normalize_path = _tmp_normalize
except Exception:  # pragma: no cover

    def _fallback_normalize(p: str) -> str:
        import posixpath as _posix

        q = p.replace("\\", "/")
        q = _posix.normpath(q)
        if q.startswith("./"):
            q = q[2:]
        return q

    _graph_normalize_path = _fallback_normalize


# ---------------------------------------------------------------------------
# Internal execution helper — works with real Neo4j driver and with
# MagicMock / fake drivers used in unit tests.
# ---------------------------------------------------------------------------

def _execute_read(driver: Any, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute a read-only Cypher query and return rows as list of dicts."""

    def _work(tx: Any) -> list[dict[str, Any]]:
        result = tx.run(cypher, **params)
        # Neo4j Result has .data() -> list[dict]
        try:
            data = result.data()  # type: ignore[union-attr]
            if isinstance(data, list):
                return data
        except Exception:
            pass
        # Some fakes return .single() or iterable
        try:
            # collect via iteration and single
            rec = result.single()  # type: ignore[union-attr]
            if rec is None:
                return []
            # rec may be dict-like with keys
            try:
                # attempt to convert record to dict
                if hasattr(rec, "keys"):
                    return [dict(rec)]  # type: ignore[arg-type]
                if isinstance(rec, dict):
                    return [rec]
            except Exception:
                pass
            return []
        except Exception:
            pass
        try:
            return list(result)  # type: ignore[arg-type]
        except Exception:
            return []

    # Resolve database name when available
    database: str | None = None
    try:
        database = getattr(driver, "database", None)
        if database is not None and not isinstance(database, str):
            database = str(database)
    except Exception:
        database = None

    # Obtain session context manager
    session_ctx: Any = None
    try:
        if database is not None:
            try:
                session_ctx = driver.session(database=database)
            except TypeError:
                session_ctx = driver.session()
        else:
            session_ctx = driver.session()
    except Exception as exc:
        # Driver might itself be a session/tx-like mock
        try:
            return _work(driver)
        except Exception:
            raise exc

    # Use session context manager
    try:
        with session_ctx as session:  # type: ignore[union-attr]
            # Prefer execute_read when available
            try:
                execute_read = getattr(session, "execute_read", None)
                if callable(execute_read):
                    return execute_read(_work)  # type: ignore[no-any-return]
            except Exception:
                pass
            # Fallback: treat session as tx
            return _work(session)
    except Exception as exc:
        # Re-raise validation errors unchanged
        if isinstance(exc, GraphValidationError):
            raise
        # If the underlying mapping raises, surface it
        raise


def _validate_repository_id(repository_id: str) -> str:
    if not repository_id or not isinstance(repository_id, str) or not repository_id.strip():
        raise GraphValidationError("repository_id must be a non-empty string")
    return repository_id.strip()


def _validate_path(path: str) -> str:
    if not path or not isinstance(path, str) or not path.strip():
        raise GraphValidationError("path must be a non-empty string")
    return path.strip()


def _row_get(row: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in row:
        return row[key]
    # Some drivers nest node under key like "f" or "c"
    for v in row.values():
        if isinstance(v, dict) and key in v:
            return v[key]
        # Neo4j Node-like objects
        try:
            if hasattr(v, "get") and callable(getattr(v, "get")):
                val = v.get(key)  # type: ignore[union-attr]
                if val is not None:
                    return val
        except Exception:
            pass
        try:
            if hasattr(v, "__getitem__") and key in v:  # type: ignore[operator]
                return v[key]  # type: ignore[index]
        except Exception:
            pass
    return default


def _extract_file_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    # Neo4j Node
    try:
        if hasattr(value, "keys"):
            return {k: value[k] for k in value.keys()}  # type: ignore[union-attr,index]
    except Exception:
        pass
    try:
        return dict(value)  # type: ignore[arg-type]
    except Exception:
        return {"value": value}


def _node_to_file(row: dict[str, Any], key: str = "f") -> dict[str, Any] | None:
    val = row.get(key)
    if val is None:
        # Try to find any File-like node in row
        for k, v in row.items():
            if isinstance(v, dict) and "path" in v:
                return _extract_file_dict(v)
            try:
                if hasattr(v, "keys"):
                    d = {kk: v[kk] for kk in v.keys()}  # type: ignore[union-attr]
                    if "path" in d or "id" in d:
                        return d
            except Exception:
                pass
        # Row itself may be the file dict
        if "path" in row or "id" in row or "repository_id" in row:
            return _extract_file_dict(row)
        return None
    return _extract_file_dict(val)


# ---------------------------------------------------------------------------
# Cypher templates — all parameterized, repository_id scoped, read-only.
# No writes; only MATCH / WHERE / RETURN / WITH / ORDER BY / count.
# Each template contains "repository_id" and "$repository_id" and a WHERE
# clause to satisfy isolation checks.
# ---------------------------------------------------------------------------

_GET_FILE_CYPHER = """
MATCH (f:File)
WHERE f.repository_id = $repository_id AND (f.path = $path OR f.canonical_path = $path OR f.current_path = $path)
RETURN f
LIMIT 1
"""

_GET_COMMITS_TOUCHING_FILE_CYPHER = """
MATCH (c:Commit)-[r:MODIFIED]->(f:File)
WHERE c.repository_id = $repository_id AND f.repository_id = $repository_id AND (f.path = $path OR f.canonical_path = $path OR f.current_path = $path)
RETURN c.hash AS hash, c.date AS date, c.message AS message, c.is_fix AS is_fix, c.is_revert AS is_revert, c.fix_reason AS fix_reason, r.additions AS additions, r.deletions AS deletions, c.id AS id, c.repository_id AS repository_id
ORDER BY c.date ASC
"""

_GET_FIX_COMMITS_FOR_FILE_CYPHER = """
MATCH (c:Commit)-[r:MODIFIED]->(f:File)
WHERE c.repository_id = $repository_id AND f.repository_id = $repository_id AND c.is_fix = true AND (f.path = $path OR f.canonical_path = $path OR f.current_path = $path)
RETURN c.hash AS hash, c.date AS date, c.message AS message, c.is_fix AS is_fix, c.fix_reason AS fix_reason, c.id AS id, c.repository_id AS repository_id
ORDER BY c.date ASC
"""

_GET_COCHANGE_NEIGHBORS_CYPHER = """
MATCH (target:File)-[r:CO_CHANGED_WITH|CO_CHANGES]-(neighbor:File)
WHERE target.repository_id = $repository_id AND neighbor.repository_id = $repository_id AND (target.path = $path OR target.canonical_path = $path OR target.current_path = $path)
RETURN neighbor.path AS path, neighbor.canonical_path AS canonical_path, neighbor.current_path AS current_path, neighbor.id AS id, neighbor.repository_id AS repository_id, neighbor.is_test AS is_test, neighbor.language AS language, r.count AS count, r.weight AS weight, r.confidence AS confidence
ORDER BY count DESC, path ASC
"""

_GET_IMPORTERS_CYPHER = """
MATCH (importer:File)-[r:IMPORTS]->(target:File)
WHERE importer.repository_id = $repository_id AND target.repository_id = $repository_id AND (target.path = $path OR target.canonical_path = $path OR target.current_path = $path)
RETURN importer.path AS path, importer.canonical_path AS canonical_path, importer.current_path AS current_path, importer.id AS id, importer.repository_id AS repository_id, importer.is_test AS is_test, importer.language AS language
ORDER BY path ASC
"""

_GET_DEPENDENCIES_CYPHER = """
MATCH (target:File)-[r:IMPORTS]->(dep:File)
WHERE target.repository_id = $repository_id AND dep.repository_id = $repository_id AND (target.path = $path OR target.canonical_path = $path OR target.current_path = $path)
RETURN dep.path AS path, dep.canonical_path AS canonical_path, dep.current_path AS current_path, dep.id AS id, dep.repository_id AS repository_id, dep.is_test AS is_test, dep.language AS language
ORDER BY path ASC
"""

_GET_TEST_CANDIDATES_CYPHER = """
MATCH (f:File)
WHERE f.repository_id = $repository_id AND (f.is_test = true OR f.path STARTS WITH "tests/" OR f.canonical_path STARTS WITH "tests/" OR f.path STARTS WITH "test/" OR f.canonical_path STARTS WITH "test/" OR f.path STARTS WITH "test_" OR f.canonical_path STARTS WITH "test_" OR f.path ENDS WITH "_test.py" OR f.canonical_path ENDS WITH "_test.py" OR f.path ENDS WITH "_tests.py" OR f.canonical_path ENDS WITH "_tests.py" OR f.path CONTAINS "/tests/" OR f.canonical_path CONTAINS "/tests/" OR f.path CONTAINS "/test/" OR f.canonical_path CONTAINS "/test/")
RETURN f.path AS path, f.canonical_path AS canonical_path, f.current_path AS current_path, f.id AS id, f.repository_id AS repository_id, f.is_test AS is_test, f.language AS language
ORDER BY path ASC
"""

_GET_REPO_COMMIT_COUNT_CYPHER = """
MATCH (c:Commit)
WHERE c.repository_id = $repository_id
RETURN count(c) AS count
"""

_GET_COMMITS_FOR_FILE_IDS_CYPHER = """
MATCH (c:Commit)-[r:MODIFIED]->(f:File)
WHERE c.repository_id = $repository_id AND f.repository_id = $repository_id AND (f.id IN $file_ids OR f.path IN $file_ids OR f.canonical_path IN $file_ids)
RETURN DISTINCT c.hash AS hash, c.date AS date, c.message AS message, c.is_fix AS is_fix, c.fix_reason AS fix_reason, c.id AS id, c.repository_id AS repository_id
ORDER BY c.date ASC
"""

_GET_COMMITS_FOR_PATHS_CYPHER = """
MATCH (c:Commit)-[r:MODIFIED]->(f:File)
WHERE c.repository_id = $repository_id AND f.repository_id = $repository_id AND (f.path IN $file_ids OR f.canonical_path IN $file_ids OR f.current_path IN $file_ids)
RETURN DISTINCT c.hash AS hash, c.date AS date, c.message AS message, c.is_fix AS is_fix, c.fix_reason AS fix_reason, c.id AS id, c.repository_id AS repository_id
ORDER BY c.date ASC
"""

# Additional helpers for completeness (used by analytics layers)

_GET_ALL_FILES_CYPHER = """
MATCH (f:File)
WHERE f.repository_id = $repository_id
RETURN f.path AS path, f.canonical_path AS canonical_path, f.current_path AS current_path, f.id AS id, f.repository_id AS repository_id, f.is_test AS is_test, f.language AS language
ORDER BY path ASC
"""

_GET_FILE_BY_ID_CYPHER = """
MATCH (f:File)
WHERE f.repository_id = $repository_id AND f.id = $file_id
RETURN f
LIMIT 1
"""


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_file(driver: Any, repository_id: str, path: str) -> dict[str, Any] | None:
    """Return file node for repository_id + path or None if not found.

    Looks up by path, canonical_path and current_path while scoping every
    lookup with repository_id.
    """
    repo = _validate_repository_id(repository_id)
    p = _validate_path(path)
    # Keep raw path for param; also handle normalization alias via Cypher OR
    rows = _execute_read(driver, _GET_FILE_CYPHER, {"repository_id": repo, "path": p})
    if not rows:
        return None
    row = rows[0]
    node = _node_to_file(row, "f")
    if node:
        return node
    # Fallback: row may already contain file fields directly
    # Try to extract via _row_get
    if "path" in row:
        return _extract_file_dict(row)
    for v in row.values():
        d = _extract_file_dict(v)
        if d and ("path" in d or "id" in d):
            return d
    return None


def get_file_by_id(driver: Any, repository_id: str, file_id: str) -> dict[str, Any] | None:
    """Return file node by id scoped to repository_id."""
    repo = _validate_repository_id(repository_id)
    if not file_id or not isinstance(file_id, str) or not file_id.strip():
        raise GraphValidationError("file_id must be a non-empty string")
    fid = file_id.strip()
    rows = _execute_read(driver, _GET_FILE_BY_ID_CYPHER, {"repository_id": repo, "file_id": fid})
    if not rows:
        return None
    return _node_to_file(rows[0], "f") or _extract_file_dict(rows[0].get("f", rows[0]))


def get_commits_touching_file(driver: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    """Return commits that modified the given file, ordered by date."""
    repo = _validate_repository_id(repository_id)
    p = _validate_path(path)
    rows = _execute_read(driver, _GET_COMMITS_TOUCHING_FILE_CYPHER, {"repository_id": repo, "path": p})
    out: list[dict[str, Any]] = []
    for r in rows:
        # Rows already projected as scalars; keep as dict
        if isinstance(r, dict):
            # Neo4j may nest; flatten
            rec: dict[str, Any] = {}
            for k in ("hash", "date", "message", "is_fix", "is_revert", "fix_reason", "additions", "deletions", "id", "repository_id"):
                if k in r:
                    rec[k] = r[k]
                else:
                    rec[k] = _row_get(r, k)
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_fix_commits_for_file(driver: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    """Return commits with is_fix=true that touched the given file."""
    repo = _validate_repository_id(repository_id)
    p = _validate_path(path)
    rows = _execute_read(driver, _GET_FIX_COMMITS_FOR_FILE_CYPHER, {"repository_id": repo, "path": p})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("hash", "date", "message", "is_fix", "fix_reason", "id", "repository_id"):
                rec[k] = r.get(k, _row_get(r, k))
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_cochange_neighbors(driver: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    """Return files that co-changed with target, with stored count/weight/confidence."""
    repo = _validate_repository_id(repository_id)
    p = _validate_path(path)
    rows = _execute_read(driver, _GET_COCHANGE_NEIGHBORS_CYPHER, {"repository_id": repo, "path": p})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("path", "canonical_path", "current_path", "id", "repository_id", "is_test", "language", "count", "weight", "confidence"):
                rec[k] = r.get(k, _row_get(r, k))
            # Normalize path fallback
            if not rec.get("path") and rec.get("canonical_path"):
                rec["path"] = rec["canonical_path"]
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_importers(driver: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    """Return files that import the target (incoming IMPORTS edges)."""
    repo = _validate_repository_id(repository_id)
    p = _validate_path(path)
    rows = _execute_read(driver, _GET_IMPORTERS_CYPHER, {"repository_id": repo, "path": p})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("path", "canonical_path", "current_path", "id", "repository_id", "is_test", "language"):
                rec[k] = r.get(k, _row_get(r, k))
            if not rec.get("path") and rec.get("canonical_path"):
                rec["path"] = rec["canonical_path"]
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_dependencies(driver: Any, repository_id: str, path: str) -> list[dict[str, Any]]:
    """Return files that target imports (outgoing IMPORTS edges)."""
    repo = _validate_repository_id(repository_id)
    p = _validate_path(path)
    rows = _execute_read(driver, _GET_DEPENDENCIES_CYPHER, {"repository_id": repo, "path": p})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("path", "canonical_path", "current_path", "id", "repository_id", "is_test", "language"):
                rec[k] = r.get(k, _row_get(r, k))
            if not rec.get("path") and rec.get("canonical_path"):
                rec["path"] = rec["canonical_path"]
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_test_candidates(driver: Any, repository_id: str) -> list[dict[str, Any]]:
    """Return candidate test files for the repository.

    Filters by is_test flag or naming patterns (tests/**, test_*, *_test.py,
    etc.) while scoping to repository_id.
    """
    repo = _validate_repository_id(repository_id)
    rows = _execute_read(driver, _GET_TEST_CANDIDATES_CYPHER, {"repository_id": repo})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("path", "canonical_path", "current_path", "id", "repository_id", "is_test", "language"):
                rec[k] = r.get(k, _row_get(r, k))
            if not rec.get("path") and rec.get("canonical_path"):
                rec["path"] = rec["canonical_path"]
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_repo_commit_count(driver: Any, repository_id: str) -> int:
    """Return total commit count for repository_id."""
    repo = _validate_repository_id(repository_id)
    rows = _execute_read(driver, _GET_REPO_COMMIT_COUNT_CYPHER, {"repository_id": repo})
    if not rows:
        return 0
    row = rows[0]
    val = row.get("count")
    if val is None:
        val = _row_get(row, "count", 0)
    try:
        return int(val) if val is not None else 0
    except Exception:
        return 0


def get_commits_for_file_ids(driver: Any, repository_id: str, file_ids: list[str]) -> list[dict[str, Any]]:
    """Return distinct commits that touched any of the given file ids/paths.

    Accepts file ids or canonical paths; matches on id, path and
    canonical_path while keeping repository_id scoping.
    """
    repo = _validate_repository_id(repository_id)
    if not isinstance(file_ids, list):
        raise GraphValidationError("file_ids must be a list of strings")
    if not file_ids:
        return []
    cleaned: list[str] = []
    for fid in file_ids:
        if isinstance(fid, str) and fid.strip():
            cleaned.append(fid.strip())
    if not cleaned:
        return []
    rows = _execute_read(driver, _GET_COMMITS_FOR_FILE_IDS_CYPHER, {"repository_id": repo, "file_ids": cleaned})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("hash", "date", "message", "is_fix", "fix_reason", "id", "repository_id"):
                rec[k] = r.get(k, _row_get(r, k))
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_commits_for_file_paths(driver: Any, repository_id: str, file_paths: list[str]) -> list[dict[str, Any]]:
    """Alias that matches by path for callers that pass paths not ids."""
    repo = _validate_repository_id(repository_id)
    if not isinstance(file_paths, list):
        raise GraphValidationError("file_paths must be a list of strings")
    if not file_paths:
        return []
    cleaned: list[str] = [p.strip() for p in file_paths if isinstance(p, str) and p.strip()]
    if not cleaned:
        return []
    rows = _execute_read(driver, _GET_COMMITS_FOR_PATHS_CYPHER, {"repository_id": repo, "file_ids": cleaned})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("hash", "date", "message", "is_fix", "fix_reason", "id", "repository_id"):
                rec[k] = r.get(k, _row_get(r, k))
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


def get_all_files(driver: Any, repository_id: str) -> list[dict[str, Any]]:
    """Return all File nodes for repository_id — helper for normalization."""
    repo = _validate_repository_id(repository_id)
    rows = _execute_read(driver, _GET_ALL_FILES_CYPHER, {"repository_id": repo})
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            rec: dict[str, Any] = {}
            for k in ("path", "canonical_path", "current_path", "id", "repository_id", "is_test", "language"):
                rec[k] = r.get(k, _row_get(r, k))
            if not rec.get("path") and rec.get("canonical_path"):
                rec["path"] = rec["canonical_path"]
            out.append(rec)
        else:
            out.append(_extract_file_dict(r))
    return out


__all__ = [
    "get_file",
    "get_file_by_id",
    "get_commits_touching_file",
    "get_fix_commits_for_file",
    "get_cochange_neighbors",
    "get_importers",
    "get_dependencies",
    "get_test_candidates",
    "get_repo_commit_count",
    "get_commits_for_file_ids",
    "get_commits_for_file_paths",
    "get_all_files",
]
