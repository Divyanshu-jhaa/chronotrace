"""Commit entity persistence."""

from __future__ import annotations

from typing import Any

from ..errors import GraphValidationError
from ..models import Commit
from .base import _execute_write, map_neo4j_error

_UPSERT_COMMIT_CYPHER = """
MERGE (c:Commit {id: $id, repository_id: $repository_id})
ON CREATE SET c += $props, c.created_at = datetime()
ON MATCH SET c += $props, c.updated_at = datetime()
RETURN c
"""

_BATCH_UPSERT_COMMIT_CYPHER = """
UNWIND $batch AS row
MERGE (c:Commit {id: row.id, repository_id: row.repository_id})
ON CREATE SET c += row.props, c.created_at = datetime()
ON MATCH SET c += row.props, c.updated_at = datetime()
"""

_GET_COMMIT_CYPHER = """
MATCH (c:Commit {id: $id, repository_id: $repository_id})
RETURN c
"""

_LIST_COMMITS_CYPHER = """
MATCH (c:Commit {repository_id: $repository_id})
RETURN c ORDER BY c.date
"""

_GET_BY_HASH_CYPHER = """
MATCH (c:Commit {hash: $hash, repository_id: $repository_id})
RETURN c
"""


class CommitRepository:
    def __init__(self, driver: Any) -> None:
        self._driver = driver

    def _validate(self, commit: Commit) -> None:
        if not commit.id or not commit.repository_id or not commit.hash:
            raise GraphValidationError("Commit requires id, repository_id, and hash")

    def upsert(self, commit: Commit) -> Commit:
        self._validate(commit)
        props = {
            "hash": commit.hash,
            "date": commit.date,
            "message": commit.message,
            "is_fix": commit.is_fix,
            "is_revert": commit.is_revert,
            "changed_file_count": commit.changed_file_count,
            "author_id": commit.author_id,
        }

        def _work(tx: Any) -> Any:
            result = tx.run(
                _UPSERT_COMMIT_CYPHER,
                id=commit.id,
                repository_id=commit.repository_id,
                props=props,
            )
            try:
                result.consume()
            except AttributeError:
                pass
            return commit

        return _execute_write(self._driver, _work)  # type: ignore[no-any-return]

    def upsert_batch(self, repository_id: str, commits: list[Commit]) -> int:
        if not repository_id:
            raise GraphValidationError("repository_id must not be empty")
        if not commits:
            return 0
        for c in commits:
            self._validate(c)
            if c.repository_id != repository_id:
                raise GraphValidationError(
                    f"Commit {c.id} belongs to {c.repository_id!r}, expected {repository_id!r}"
                )
        batch = [
            {
                "id": c.id,
                "repository_id": c.repository_id,
                "props": {
                    "hash": c.hash,
                    "date": c.date,
                    "message": c.message,
                    "is_fix": c.is_fix,
                    "is_revert": c.is_revert,
                    "changed_file_count": c.changed_file_count,
                    "author_id": c.author_id,
                },
            }
            for c in commits
        ]
        batch_size = getattr(self._driver, "batch_size", 500)
        total = 0
        for chunk in _chunk_batch(batch, batch_size):

            def _work(tx: Any, _chunk: list[dict[str, Any]] = chunk) -> None:
                res = tx.run(_BATCH_UPSERT_COMMIT_CYPHER, batch=_chunk)
                try:
                    res.consume()
                except AttributeError:
                    pass

            _execute_write(self._driver, _work)
            total += len(chunk)
        return total

    def get(self, commit_id: str, repository_id: str) -> Commit | None:
        if not commit_id or not repository_id:
            raise GraphValidationError("commit_id and repository_id must not be empty")

        def _work(tx: Any) -> Commit | None:
            result = tx.run(_GET_COMMIT_CYPHER, id=commit_id, repository_id=repository_id)
            try:
                record = result.single()
            except AttributeError:
                data = result.data() if hasattr(result, "data") else []
                if not data:
                    return None
                first = data[0]
                return Commit.from_record(first["c"] if "c" in first else first)
            if record is None:
                return None
            try:
                node = record["c"]
                props = dict(node) if hasattr(node, "keys") else node
                return Commit.from_record(props if isinstance(props, dict) else dict(node))
            except Exception:
                try:
                    data = result.data()
                    if data:
                        return Commit.from_record(data[0]["c"] if "c" in data[0] else data[0])
                except Exception:
                    pass
                return None

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except GraphValidationError:
            raise
        except Exception as e:
            raise map_neo4j_error(e) from e

    def get_by_hash(self, repository_id: str, commit_hash: str) -> Commit | None:
        if not repository_id or not commit_hash:
            raise GraphValidationError("repository_id and hash must not be empty")

        def _work(tx: Any) -> Commit | None:
            result = tx.run(
                _GET_BY_HASH_CYPHER, repository_id=repository_id, hash=commit_hash.strip().lower()
            )
            try:
                record = result.single()
            except AttributeError:
                data = result.data() if hasattr(result, "data") else []
                if not data:
                    return None
                first = data[0]
                return Commit.from_record(first["c"] if "c" in first else first)
            if record is None:
                return None
            try:
                node = record["c"]
                props = dict(node) if hasattr(node, "keys") else node
                return Commit.from_record(props if isinstance(props, dict) else dict(node))
            except Exception:
                return None

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except GraphValidationError:
            raise
        except Exception as e:
            raise map_neo4j_error(e) from e

    def list_by_repository(self, repository_id: str) -> list[Commit]:
        if not repository_id:
            raise GraphValidationError("repository_id must not be empty")

        def _work(tx: Any) -> list[Commit]:
            result = tx.run(_LIST_COMMITS_CYPHER, repository_id=repository_id)
            try:
                data = result.data()
                out: list[Commit] = []
                for row in data:
                    rec = row["c"] if "c" in row else row
                    if hasattr(rec, "keys"):
                        rec = dict(rec)
                    out.append(Commit.from_record(rec))
                return out
            except AttributeError:
                return []

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except GraphValidationError:
            raise
        except Exception as e:
            raise map_neo4j_error(e) from e


def _chunk_batch(batch: list[Any], size: int) -> Any:  # type: ignore[misc]
    for i in range(0, len(batch), size):
        yield batch[i : i + size]