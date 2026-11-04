"""Fixed fixture for iter-5 analytics (Task 7.1).

Seeded repository ``R-fixture`` with:

* files: ``a.py``, ``b.py``, ``c.py``, ``tests/test_a.py``
* commits ``C1..C5`` across half-life boundaries (half_life=30d, now=2024-04-01)
* ``is_fix`` marks on ``C2`` and ``C4``
* ``MODIFIED`` edges with additions/deletions
* ``CO_CHANGES`` edges (co-changed-with, weight/count/confidence)
* ``IMPORTS`` edges (``b.py -> a.py``, ``tests/test_a.py -> a.py``)

Provides helpers:

* :func:`get_fake_graph` — in-memory fake implementing the analytics gateway interface
* :func:`seed_neo4j` — idempotently seeds a disposable Neo4j via :class:`GraphGateway`
* :func:`load_expected` — loads ``expected.json``
* constants :data:`REPO_ID`, :data:`HALF_LIFE_DAYS`, :data:`NOW`, :data:`FILES`, etc.

All analytics are deterministic for the same ``(graph, half_life, now)`` tuple.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ID: str = "R-fixture"
HALF_LIFE_DAYS: float = 30.0
NOW: datetime = datetime(2024, 4, 1, 0, 0, 0, tzinfo=timezone.utc)

# File nodes
FILES: list[dict[str, Any]] = [
    {
        "path": "a.py",
        "canonical_path": "a.py",
        "current_path": "a.py",
        "id": f"{REPO_ID}:a.py",
        "repository_id": REPO_ID,
        "is_test": False,
        "language": "python",
    },
    {
        "path": "b.py",
        "canonical_path": "b.py",
        "current_path": "b.py",
        "id": f"{REPO_ID}:b.py",
        "repository_id": REPO_ID,
        "is_test": False,
        "language": "python",
    },
    {
        "path": "c.py",
        "canonical_path": "c.py",
        "current_path": "c.py",
        "id": f"{REPO_ID}:c.py",
        "repository_id": REPO_ID,
        "is_test": False,
        "language": "python",
    },
    {
        "path": "tests/test_a.py",
        "canonical_path": "tests/test_a.py",
        "current_path": "tests/test_a.py",
        "id": f"{REPO_ID}:tests/test_a.py",
        "repository_id": REPO_ID,
        "is_test": True,
        "language": "python",
    },
]

# Commit hashes (40 hex chars, distinct)
C1_HASH = "1" * 40
C2_HASH = "2" * 40
C3_HASH = "3" * 40
C4_HASH = "4" * 40
C5_HASH = "5" * 40

# Commit definitions
COMMITS: list[dict[str, Any]] = [
    {
        "hash": C1_HASH,
        "date": "2024-01-01T00:00:00+00:00",
        "message": "feat: initial commit",
        "is_fix": False,
        "fix_reason": None,
        "author": "alice@example.com",
        "modified": ["a.py", "b.py"],
    },
    {
        "hash": C2_HASH,
        "date": "2024-02-01T00:00:00+00:00",
        "message": "fix: handle edge case in a",
        "is_fix": True,
        "fix_reason": "pattern='fix' matched fix",
        "author": "bob@example.com",
        "modified": ["a.py", "c.py", "tests/test_a.py"],
    },
    {
        "hash": C3_HASH,
        "date": "2024-03-02T00:00:00+00:00",  # exactly 30d before NOW => weight 0.5
        "message": "refactor: update b and c",
        "is_fix": False,
        "fix_reason": None,
        "author": "alice@example.com",
        "modified": ["a.py", "b.py", "c.py"],
    },
    {
        "hash": C4_HASH,
        "date": "2024-03-25T00:00:00+00:00",  # 7d before NOW
        "message": "fix: regression in a b c coupling",
        "is_fix": True,
        "fix_reason": "pattern='fix' matched fix",
        "author": "carol@example.com",
        "modified": ["a.py", "b.py", "c.py"],
    },
    {
        "hash": C5_HASH,
        "date": "2024-04-01T00:00:00+00:00",  # age 0 => weight 1.0
        "message": "feat: polish a and b",
        "is_fix": False,
        "fix_reason": None,
        "author": "bob@example.com",
        "modified": ["a.py", "b.py", "tests/test_a.py"],
    },
]

# Per-commit, per-file line stats (additions, deletions)
# Use dict hash -> list of (path, additions, deletions)
MODIFIED: dict[str, list[tuple[str, int, int]]] = {
    C1_HASH: [("a.py", 10, 2), ("b.py", 5, 0)],
    C2_HASH: [("a.py", 20, 5), ("c.py", 3, 0), ("tests/test_a.py", 10, 0)],
    C3_HASH: [("a.py", 2, 0), ("b.py", 15, 3), ("c.py", 1, 0)],
    C4_HASH: [("a.py", 30, 10), ("b.py", 2, 0), ("c.py", 4, 0)],
    C5_HASH: [("a.py", 1, 0), ("b.py", 1, 0), ("tests/test_a.py", 2, 0)],
}

# CO_CHANGES edges (undirected, stored once, but fake returns neighbors either way)
# Count = number of commits where both files co-occurred (derived from MODIFIED)
CO_CHANGES: list[dict[str, Any]] = [
    {"source": "a.py", "target": "b.py", "count": 4, "weight": 4.0, "confidence": 0.8},
    {"source": "a.py", "target": "c.py", "count": 3, "weight": 3.0, "confidence": 0.6},
    {"source": "a.py", "target": "tests/test_a.py", "count": 2, "weight": 2.0, "confidence": 0.4},
    {"source": "b.py", "target": "c.py", "count": 2, "weight": 2.0, "confidence": 0.4},
    {"source": "b.py", "target": "tests/test_a.py", "count": 1, "weight": 1.0, "confidence": 0.2},
    {"source": "c.py", "target": "tests/test_a.py", "count": 1, "weight": 1.0, "confidence": 0.2},
]

# IMPORTS edges (directed)
IMPORTS: list[tuple[str, str]] = [
    ("b.py", "a.py"),
    ("tests/test_a.py", "a.py"),
]

# Helper maps
_COMMIT_BY_HASH: dict[str, dict[str, Any]] = {c["hash"]: c for c in COMMITS}
_FILE_BY_PATH: dict[str, dict[str, Any]] = {f["path"]: f for f in FILES}


def _normalize(p: str) -> str:
    if not p:
        return ""
    p = p.replace("\\", "/")
    p = posixpath.normpath(p)
    if p.startswith("./"):
        p = p[2:]
    return p


# ---------------------------------------------------------------------------
# Fake gateway for unit tests
# ---------------------------------------------------------------------------

class _FakeFileRepo:
    def __init__(self, outer: FakeAnalyticsGraph) -> None:
        self._outer = outer

    def list_by_repository(self, repository_id: str):  # type: ignore[override]
        return self._outer.list_files(repository_id)

    def get_by_path(self, repository_id: str, path: str):
        return self._outer.get_file_by_path(repository_id, path)

    def get_file_by_path(self, repository_id: str, path: str):
        return self._outer.get_file_by_path(repository_id, path)

    # alias for service lookup
    def get_file(self, file_id: str, repository_id: str):
        # file_id may be repo:path
        if ":" in file_id:
            maybe = file_id.split(":", 1)[-1]
            return self._outer.get_file_by_path(repository_id, maybe)
        return self._outer.get_file_by_path(repository_id, file_id)


class _FakeCommitRepo:
    def __init__(self, outer: FakeAnalyticsGraph) -> None:
        self._outer = outer

    def list_by_repository(self, repository_id: str):  # type: ignore[override]
        return self._outer.list_commits(repository_id)

    def get_fix_commits_for_file(self, repository_id: str, path: str):
        return self._outer.get_fix_commits_for_file(repository_id, path)

    def get_commits_touching_file(self, repository_id: str, path: str):
        return self._outer.get_commits_touching_file(repository_id, path)


class _FakeRelationshipRepo:
    def __init__(self, outer: FakeAnalyticsGraph) -> None:
        self._outer = outer

    def get_cochange_neighbors(self, repository_id: str, path: str):
        return self._outer.get_cochange_neighbors(repository_id, path)

    def get_co_change_neighbors(self, repository_id: str, path: str):
        return self._outer.get_cochange_neighbors(repository_id, path)

    def get_importers(self, repository_id: str, path: str):
        return self._outer.get_importers(repository_id, path)

    def get_dependencies(self, repository_id: str, path: str):
        return self._outer.get_dependencies(repository_id, path)

    def get_importers_for_file(self, repository_id: str, path: str):
        return self._outer.get_importers(repository_id, path)


class FakeAnalyticsGraph:
    """In-memory fake that implements the analytics gateway interface.

    Implements repository-scoped, parameterized reads without a Neo4j driver.
    Supports both direct method calls and nested ``graph.files`` / ``commits``
    / ``relationships`` lookups used by the analytics modules' fallback logic,
    and exposes storage attributes scanned by fallback introspection
    (``_fake_commits``, ``_imports``, ``_co_changes``, etc.) for maximum
    compatibility.
    """

    def __init__(self, repository_id: str = REPO_ID) -> None:
        self._expected_repo = repository_id
        # Data storage (canonical)
        self._files_data: list[dict[str, Any]] = [dict(f) for f in FILES]
        self._commits_data: list[dict[str, Any]] = [dict(c) for c in COMMITS]
        self._modified_map: dict[str, list[tuple[str, int, int]]] = {
            h: list(v) for h, v in MODIFIED.items()
        }
        self._co_changes_data: list[dict[str, Any]] = [dict(e) for e in CO_CHANGES]
        self._imports_data: list[tuple[str, str]] = list(IMPORTS)

        # Flat commit hash -> modified list for fallback
        self._commit_file_map: dict[str, list[str]] = {
            c["hash"]: list(c["modified"]) for c in self._commits_data
        }
        # Reverse: file path -> commit hashes
        self._file_commits: dict[str, list[str]] = {}
        for c in self._commits_data:
            for p in c["modified"]:
                self._file_commits.setdefault(_normalize(p), []).append(c["hash"])

        # Expose attributes that scanning fallbacks look for
        self._fake_commits: list[dict[str, Any]] = self._commits_data
        self._fake_commit_to_files: dict[str, list[str]] = dict(self._commit_file_map)
        self._fake_files: list[dict[str, Any]] = self._files_data
        self._files: list[dict[str, Any]] = self._files_data
        self._commits: list[dict[str, Any]] = self._commits_data
        self._imports: list[Any] = [(s, t) for s, t in self._imports_data]
        self._import_edges: list[Any] = [{"source": s, "target": t, "repository_id": REPO_ID} for s, t in self._imports_data]
        self._co_changes: dict[tuple[str, str], dict[str, Any]] = {}
        self._co_changed_with: dict[tuple[str, str], int] = {}
        for e in self._co_changes_data:
            src = _normalize(e["source"])
            dst = _normalize(e["target"])
            # store sorted tuple for undirected lookup
            key = (src, dst)
            rev = (dst, src)
            self._co_changes[key] = e
            self._co_changes[rev] = e
            self._co_changed_with[key] = int(e["count"])
            self._co_changed_with[rev] = int(e["count"])
        # Additional aliases for various fallback attribute names
        self.co_changes: dict[tuple[str, str], dict[str, Any]] = self._co_changes
        self._co_change_map: dict[tuple[str, str], dict[str, Any]] = self._co_changes
        self._fake_import_edges: dict[str, list[dict[str, str]]] = {
            REPO_ID: [{"source": s, "target": t} for s, t in self._imports_data]
        }
        self.imports: list[tuple[str, str]] = self._imports  # type: ignore[assignment]
        self.import_edges: list[tuple[str, str]] = self._imports  # type: ignore

        # Nested repo facades for graph.files / graph.commits / graph.relationships
        self.files = _FakeFileRepo(self)  # type: ignore[assignment]
        self.commits = _FakeCommitRepo(self)  # type: ignore[assignment]
        self.relationships = _FakeRelationshipRepo(self)  # type: ignore[assignment]
        # Some code checks graph.file_repo / graph.commit_repo
        self.file_repo = self.files  # type: ignore[assignment]
        self.commit_repo = self.commits  # type: ignore[assignment]
        # Also expose as graph graph
        self.graph = self  # type: ignore[assignment]

        # Diagnostics / commit count helpers
        self._diagnostics: dict[str, Any] = {"excluded_paths": 0, "parser_failures": 0, "files_failed": 0}

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    def list_files(self, repository_id: str) -> list[dict[str, Any]]:
        if _normalize(repository_id) != _normalize(REPO_ID) and repository_id != REPO_ID:
            # allow exact match only; return empty for other repos (isolation)
            if repository_id != REPO_ID:
                return []
        return [dict(f) for f in self._files_data]

    def list_by_repository(self, repository_id: str) -> list[dict[str, Any]]:
        # ambiguous: used for both files and commits depending on caller
        # For gateway-level call, return files; commit repo has its own override
        return self.list_files(repository_id)

    def list_commits(self, repository_id: str) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        return [dict(c) for c in self._commits_data]

    def get_all_files(self, repository_id: str) -> list[dict[str, Any]]:
        return self.list_files(repository_id)

    def get_file_by_path(self, repository_id: str, path: str) -> dict[str, Any] | None:
        if repository_id != REPO_ID:
            return None
        norm = _normalize(path)
        for f in self._files_data:
            if _normalize(f["path"]) == norm or _normalize(f.get("canonical_path", "")) == norm:
                return dict(f)
        return None

    def get_by_path(self, repository_id: str, path: str) -> dict[str, Any] | None:
        return self.get_file_by_path(repository_id, path)

    def get_file(self, file_id: str, repository_id: str) -> dict[str, Any] | None:
        # file_id may be repo:path or just path
        if ":" in file_id:
            maybe = file_id.split(":", 1)[-1]
            if "/" in maybe or maybe.endswith(".py"):
                return self.get_file_by_path(repository_id, maybe)
        return self.get_file_by_path(repository_id, file_id)

    # ------------------------------------------------------------------
    # Commit helpers
    # ------------------------------------------------------------------

    def get_commits_touching_file(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        norm = _normalize(path)
        out: list[dict[str, Any]] = []
        for c in self._commits_data:
            mods = self._commit_file_map.get(c["hash"], [])
            norm_mods = [_normalize(m) for m in mods]
            if norm not in norm_mods:
                continue
            # find per-file additions/deletions
            add = None
            dele = None
            for p, a, d in self._modified_map.get(c["hash"], []):
                if _normalize(p) == norm:
                    add = a
                    dele = d
                    break
            out.append(
                {
                    "hash": c["hash"],
                    "date": c["date"],
                    "message": c["message"],
                    "is_fix": bool(c.get("is_fix")),
                    "isFix": bool(c.get("is_fix")),
                    "fix_reason": c.get("fix_reason"),
                    "fixReason": c.get("fix_reason"),
                    "additions": add,
                    "deletions": dele,
                    "id": f"{REPO_ID}:{c['hash']}",
                    "repository_id": REPO_ID,
                }
            )
        # sort by date asc for determinism (mirrors Cypher ORDER BY)
        out.sort(key=lambda x: (x.get("date", ""), x.get("hash", "")))
        return out

    def get_fix_commits_for_file(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        all_commits = self.get_commits_touching_file(repository_id, path)
        return [c for c in all_commits if c.get("is_fix")]

    def get_fix_commits(self, repository_id: str) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        return [dict(c) for c in self._commits_data if c.get("is_fix")]

    def get_commits_for_file_ids(self, repository_id: str, file_ids: list[str]) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        # file_ids may be paths or ids
        norm_ids = set(_normalize(fid.split(":", 1)[-1] if ":" in fid and "/" in fid.split(":", 1)[-1] else fid) for fid in file_ids)
        # also handle bare paths
        added: set[str] = set()
        out: list[dict[str, Any]] = []
        for c in self._commits_data:
            mods = self._commit_file_map.get(c["hash"], [])
            for m in mods:
                if _normalize(m) in norm_ids:
                    if c["hash"] not in added:
                        added.add(c["hash"])
                        out.append(
                            {
                                "hash": c["hash"],
                                "date": c["date"],
                                "message": c["message"],
                                "is_fix": bool(c.get("is_fix")),
                                "fix_reason": c.get("fix_reason"),
                                "id": f"{REPO_ID}:{c['hash']}",
                                "repository_id": REPO_ID,
                            }
                        )
                    break
        out.sort(key=lambda x: (x.get("date", ""), x.get("hash", "")))
        return out

    def get_commits_for_file_paths(self, repository_id: str, file_paths: list[str]) -> list[dict[str, Any]]:
        return self.get_commits_for_file_ids(repository_id, file_paths)

    # ------------------------------------------------------------------
    # Co-change helpers
    # ------------------------------------------------------------------

    def get_cochange_neighbors(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        norm = _normalize(path)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for e in self._co_changes_data:
            src = _normalize(e["source"])
            dst = _normalize(e["target"])
            neighbor = None
            if src == norm:
                neighbor = dst
            elif dst == norm:
                neighbor = src
            else:
                continue
            if neighbor in seen:
                continue
            seen.add(neighbor)
            # find file node for extra fields
            f = _FILE_BY_PATH.get(neighbor) or {"path": neighbor, "canonical_path": neighbor, "id": f"{REPO_ID}:{neighbor}", "repository_id": REPO_ID, "is_test": neighbor.startswith("tests/")}
            out.append(
                {
                    "path": neighbor,
                    "canonical_path": neighbor,
                    "current_path": neighbor,
                    "id": f.get("id", f"{REPO_ID}:{neighbor}"),
                    "repository_id": REPO_ID,
                    "is_test": f.get("is_test", False),
                    "language": f.get("language", "python"),
                    "count": int(e["count"]),
                    "weight": float(e["weight"]) if e.get("weight") is not None else None,
                    "confidence": float(e["confidence"]) if e.get("confidence") is not None else None,
                }
            )
        out.sort(key=lambda x: (-int(x.get("count", 0)), x["path"]))
        return out

    def get_co_change_neighbors(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        return self.get_cochange_neighbors(repository_id, path)

    def get_co_changed(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        return self.get_cochange_neighbors(repository_id, path)

    # ------------------------------------------------------------------
    # Import helpers
    # ------------------------------------------------------------------

    def get_importers(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        norm = _normalize(path)
        out: list[dict[str, Any]] = []
        for src, dst in self._imports_data:
            if _normalize(dst) == norm:
                f = _FILE_BY_PATH.get(src, {"path": src, "canonical_path": src, "id": f"{REPO_ID}:{src}", "repository_id": REPO_ID, "is_test": src.startswith("tests/")})
                out.append(
                    {
                        "path": src,
                        "canonical_path": src,
                        "current_path": src,
                        "id": f.get("id", f"{REPO_ID}:{src}"),
                        "repository_id": REPO_ID,
                        "is_test": f.get("is_test", False),
                        "language": f.get("language", "python"),
                    }
                )
        out.sort(key=lambda x: x["path"])
        return out

    def get_importers_for_file(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        return self.get_importers(repository_id, path)

    def get_dependencies(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        norm = _normalize(path)
        out: list[dict[str, Any]] = []
        for src, dst in self._imports_data:
            if _normalize(src) == norm:
                f = _FILE_BY_PATH.get(dst, {"path": dst, "canonical_path": dst, "id": f"{REPO_ID}:{dst}", "repository_id": REPO_ID})
                out.append(
                    {
                        "path": dst,
                        "canonical_path": dst,
                        "current_path": dst,
                        "id": f.get("id", f"{REPO_ID}:{dst}"),
                        "repository_id": REPO_ID,
                        "is_test": f.get("is_test", False),
                        "language": f.get("language", "python"),
                    }
                )
        out.sort(key=lambda x: x["path"])
        return out

    def get_dependencies_for_file(self, repository_id: str, path: str) -> list[dict[str, Any]]:
        return self.get_dependencies(repository_id, path)

    # ------------------------------------------------------------------
    # Test candidate helpers
    # ------------------------------------------------------------------

    def get_test_candidates(self, repository_id: str) -> list[dict[str, Any]]:
        if repository_id != REPO_ID:
            return []
        out: list[dict[str, Any]] = []
        for f in self._files_data:
            p = f["path"]
            # is_test flag or pattern
            is_test = bool(f.get("is_test"))
            if is_test or p.startswith("tests/") or p.startswith("test_") or p.endswith("_test.py") or "/tests/" in p:
                out.append(dict(f))
        out.sort(key=lambda x: x["path"])
        return out

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    def get_repo_commit_count(self, repository_id: str) -> int:
        if repository_id != REPO_ID:
            return 0
        return len(self._commits_data)

    def get_commit_count(self, repository_id: str) -> int:
        return self.get_repo_commit_count(repository_id)

    def count_commits(self, repository_id: str) -> int:
        return self.get_repo_commit_count(repository_id)

    def get_files_for_commits(self, repository_id: str, hashes: list[str], target: str | None = None) -> dict[str, set[str]]:
        # return mapping file -> set of hashes
        if repository_id != REPO_ID:
            return {}
        norm_hashes = {h.strip().lower() for h in hashes}
        out: dict[str, set[str]] = {}
        for c in self._commits_data:
            hl = c["hash"].lower()
            if hl not in norm_hashes:
                continue
            for p in self._commit_file_map.get(c["hash"], []):
                norm_p = _normalize(p)
                if target and _normalize(target) == norm_p:
                    continue
                out.setdefault(norm_p, set()).add(c["hash"])
        return out

    # For driver fallback checks, expose .driver as None
    @property
    def driver(self):  # type: ignore[override]
        return None

    # Allow attribute scanning for cochange / imports
    def get_all_files_alt(self, repository_id: str):
        return self.list_files(repository_id)


def get_fake_graph(repository_id: str = REPO_ID) -> FakeAnalyticsGraph:
    """Return a new fake graph for the fixture repository.

    Each call returns a fresh instance so tests can mutate without
    cross-test pollution. Repository isolation is enforced: queries for
    any other ``repository_id`` return empty.
    """
    return FakeAnalyticsGraph(repository_id=repository_id)


def load_expected() -> dict[str, Any]:
    """Load ``expected.json`` for the fixture.

    Returns the parsed JSON dict containing ``criticality``, ``regression``,
    ``impact`` and ``test_reco`` expected values.
    """
    p = Path(__file__).with_name("expected.json")
    if not p.exists():
        # fallback to repo root search
        p = Path(__file__).parent / "expected.json"
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def expected_path() -> Path:
    return Path(__file__).with_name("expected.json")


# ---------------------------------------------------------------------------
# Neo4j seeding (integration)
# ---------------------------------------------------------------------------

def seed_neo4j(gateway: Any) -> None:
    """Seed the fixture into a live :class:`GraphGateway` (disposable Neo4j).

    Idempotently ``MERGE``s ``Repository``, ``File`` and ``Commit`` nodes
    and creates ``MODIFIED``, ``CO_CHANGED_WITH`` and ``IMPORTS`` and
    ``CONTAINS``/``BELONGS_TO`` relationships via the gateway's
    parameterized, repository-scoped writers. Uses only ``MERGE``-based
    writes (no raw string interpolation) so ``openspec validate --strict``
    and read-only checks remain satisfied.

    Args:
        gateway: :class:`chronotrace.graph.gateway.GraphGateway` connected
            to a disposable Neo4j (testcontainers or ``bolt://localhost:7687``).
    """
    from chronotrace.graph.models import Commit, File, Repository

    # Repository node
    repo = Repository.create(name=REPO_ID, repository_id=REPO_ID)
    gateway.repositories.upsert(repo)

    # Files
    file_objs: list[File] = []
    for f in FILES:
        file_objs.append(
            File.create(
                repository_id=REPO_ID,
                path=f["path"],
                is_test=bool(f.get("is_test")),
                language=f.get("language", "python"),
            )
        )
    gateway.files.upsert_batch(REPO_ID, file_objs)
    # Build file_id lookup
    file_id_by_path: dict[str, str] = {f.path: f.id for f in gateway.files.list_by_repository(REPO_ID)}
    # Fallback if gateway didn't persist correctly (e.g., driver isolation)
    for f in file_objs:
        if f.path not in file_id_by_path:
            file_id_by_path[f.path] = f.id

    # Commits
    commit_objs: list[Commit] = []
    for c in COMMITS:
        commit_objs.append(
            Commit.create(
                repository_id=REPO_ID,
                hash=c["hash"],
                date=c["date"],
                message=c["message"],
                is_fix=bool(c.get("is_fix")),
            )
        )
    gateway.commits.upsert_batch(REPO_ID, commit_objs)
    # Build commit_id lookup (Gateway generates id as f"{repo}:{hash}")
    commit_id_by_hash: dict[str, str] = {}
    for c in gateway.commits.list_by_repository(REPO_ID):
        # c.hash is the commit hash
        h = getattr(c, "hash", None) or getattr(c, "commit_hash", None) or ""
        cid = getattr(c, "id", None) or f"{REPO_ID}:{h}"
        if h:
            commit_id_by_hash[str(h)] = str(cid)
    for co in commit_objs:
        if co.hash not in commit_id_by_hash:
            commit_id_by_hash[co.hash] = co.id

    # Persist fixReason for fix commits via direct Cypher (Commit model doesn't carry fix_reason in to_props)
    with gateway.driver.session() as s:
        for c in COMMITS:
            if c.get("is_fix") and c.get("fix_reason"):
                cid = commit_id_by_hash.get(c["hash"])
                if cid:
                    s.run(
                        """
                        MATCH (c:Commit {id: $id, repository_id: $repo})
                        SET c.fixReason = $reason, c.is_fix = true, c.isFix = true
                        """,
                        id=cid,
                        repo=REPO_ID,
                        reason=c["fix_reason"],
                    ).consume()
            # Also ensure is_fix flag correct for non-fix commits
            elif not c.get("is_fix"):
                cid = commit_id_by_hash.get(c["hash"])
                if cid:
                    s.run(
                        "MATCH (c:Commit {id:$id, repository_id:$repo}) SET c.is_fix = false, c.isFix = false",
                        id=cid,
                        repo=REPO_ID,
                    ).consume()

    # MODIFIED edges (with line stats)
    modified_rows: list[dict[str, Any]] = []
    for h, entries in MODIFIED.items():
        cid = commit_id_by_hash.get(h)
        if not cid:
            continue
        for path, add, dele in entries:
            fid = file_id_by_path.get(path)
            if not fid:
                # try normalized lookup
                for k, v in file_id_by_path.items():
                    if _normalize(k) == _normalize(path):
                        fid = v
                        break
            if not fid:
                continue
            modified_rows.append(
                {
                    "commit_id": cid,
                    "file_id": fid,
                    "additions": add,
                    "deletions": dele,
                    "change_type": "MODIFIED",
                    "repository_id": REPO_ID,
                }
            )
    if modified_rows:
        gateway.relationships.batch_modified(REPO_ID, modified_rows)

    # CO_CHANGED_WITH edges
    co_rows: list[dict[str, Any]] = []
    for e in CO_CHANGES:
        src_path = e["source"]
        dst_path = e["target"]
        src_id = file_id_by_path.get(src_path)
        dst_id = file_id_by_path.get(dst_path)
        if not src_id or not dst_id:
            continue
        # Create both directions as one logical undirected edge, but store once
        # and let reads treat it as undirected (our read helpers handle both)
        # For Neo4j we store directed edge; impact reads match (File)-[r:CO_CHANGED_WITH]-(File)
        co_rows.append(
            {
                "source_id": src_id,
                "target_id": dst_id,
                "count": int(e["count"]),
                "weight": float(e["weight"]),
                "confidence": float(e["confidence"]) if e.get("confidence") is not None else None,
                "repository_id": REPO_ID,
            }
        )
    if co_rows:
        gateway.relationships.batch_co_changed(REPO_ID, co_rows)

    # IMPORTS edges
    import_rows: list[tuple[str, str]] = []
    for src_path, dst_path in IMPORTS:
        src_id = file_id_by_path.get(src_path)
        dst_id = file_id_by_path.get(dst_path)
        if src_id and dst_id:
            import_rows.append((src_id, dst_id))
    if import_rows:
        gateway.relationships.batch_imports(REPO_ID, import_rows)

    # CONTAINS and BELONGS_TO for completeness
    try:
        gateway.relationships.batch_contains(REPO_ID, list(file_id_by_path.values()))
    except Exception:
        pass
    try:
        gateway.relationships.batch_belongs_to(REPO_ID, list(commit_id_by_hash.values()))
    except Exception:
        pass
