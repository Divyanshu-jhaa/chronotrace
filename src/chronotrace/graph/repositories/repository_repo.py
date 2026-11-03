"""Repository entity persistence."""

from __future__ import annotations

from typing import Any

from ..errors import GraphValidationError
from ..models import Repository
from .base import _execute_write, map_neo4j_error

# Parameterized Cypher templates — no f-string interpolation
_UPSERT_REPOSITORY_CYPHER = """
MERGE (r:Repository {id: $id, repository_id: $repository_id})
ON CREATE SET r += $props, r.created_at = datetime()
ON MATCH SET r += $props, r.updated_at = datetime()
RETURN r
"""

_BATCH_UPSERT_REPOSITORY_CYPHER = """
UNWIND $batch AS row
MERGE (r:Repository {id: row.id, repository_id: row.repository_id})
ON CREATE SET r += row.props, r.created_at = datetime()
ON MATCH SET r += row.props, r.updated_at = datetime()
"""

_GET_REPOSITORY_CYPHER = """
MATCH (r:Repository {id: $id, repository_id: $repository_id})
RETURN r
"""

_LIST_REPOSITORIES_CYPHER = """
MATCH (r:Repository)
RETURN r
"""


class RepositoryRepository:
    """Persistence for Repository nodes."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    def upsert(self, repository: Repository) -> Repository:
        if not repository.id or not repository.repository_id:
            raise GraphValidationError("Repository requires id and repository_id")
        props = {
            "name": repository.name,
            "remote_url": repository.remote_url,
            "default_branch": repository.default_branch,
            "current_head": repository.current_head,
            "last_ingested_at": repository.last_ingested_at,
        }
        def _work(tx: Any) -> Any:
            result = tx.run(
                _UPSERT_REPOSITORY_CYPHER,
                id=repository.id,
                repository_id=repository.repository_id,
                props=props,
            )
            try:
                result.consume()
            except AttributeError:
                pass
            return repository

        return _execute_write(self._driver, _work)  # type: ignore[no-any-return]

    def upsert_batch(self, repositories: list[Repository]) -> int:
        if not repositories:
            return 0
        batch = []
        for repo in repositories:
            if not repo.id or not repo.repository_id:
                raise GraphValidationError("Repository requires id and repository_id")
            batch.append(
                {
                    "id": repo.id,
                    "repository_id": repo.repository_id,
                    "props": {
                        "name": repo.name,
                        "remote_url": repo.remote_url,
                        "default_branch": repo.default_branch,
                        "current_head": repo.current_head,
                        "last_ingested_at": repo.last_ingested_at,
                    },
                }
            )
        batch_size = getattr(self._driver, "batch_size", 500)
        total = 0

        for chunk in _chunked(batch, batch_size):

            def _work(tx: Any, _chunk: list[dict[str, Any]] = chunk) -> None:
                res = tx.run(_BATCH_UPSERT_REPOSITORY_CYPHER, batch=_chunk)
                try:
                    res.consume()
                except AttributeError:
                    pass

            _execute_write(self._driver, _work)
            total += len(chunk)
        return total

    def get(self, repository_id: str) -> Repository | None:
        if not repository_id:
            raise GraphValidationError("repository_id must not be empty")

        def _work(tx: Any) -> Repository | None:
            result = tx.run(
                _GET_REPOSITORY_CYPHER,
                id=repository_id,
                repository_id=repository_id,
            )
            try:
                record = result.single()
            except AttributeError:
                # Mock fallback: data()
                data = result.data() if hasattr(result, "data") else []
                if not data:
                    return None
                # Extract first node's properties
                # Data shape varies; try to find 'r'
                first = data[0]
                if "r" in first:
                    return Repository.from_record(first["r"])
                return Repository.from_record(first)
            if record is None:
                return None
            # Retrieve node properties
            try:
                node = record["r"]
                # neo4j Node object behaves like dict
                props = dict(node) if hasattr(node, "keys") else node
                if isinstance(props, dict) and "id" not in props:
                    # Try record.data fallback
                    pass
                return Repository.from_record(props if isinstance(props, dict) else dict(node))
            except Exception:
                # Fallback to single record dict
                try:
                    data = result.data()
                    if data:
                        return Repository.from_record(data[0]["r"] if "r" in data[0] else data[0])
                except Exception:
                    pass
                return None

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except Exception as e:
            # Map errors but preserve validation
            if isinstance(e, GraphValidationError):
                raise
            raise map_neo4j_error(e) from e


def _chunked(batch: list[Any], size: int) -> Any:  # type: ignore[misc]
    for i in range(0, len(batch), size):
        yield batch[i : i + size]