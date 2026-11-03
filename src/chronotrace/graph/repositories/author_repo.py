"""Author entity persistence."""

from __future__ import annotations

from typing import Any

from ..errors import GraphValidationError
from ..models import Author
from .base import _execute_write, map_neo4j_error

_UPSERT_AUTHOR_CYPHER = """
MERGE (a:Author {id: $id, repository_id: $repository_id})
ON CREATE SET a += $props, a.created_at = datetime()
ON MATCH SET a += $props, a.updated_at = datetime()
RETURN a
"""

_BATCH_UPSERT_AUTHOR_CYPHER = """
UNWIND $batch AS row
MERGE (a:Author {id: row.id, repository_id: row.repository_id})
ON CREATE SET a += row.props, a.created_at = datetime()
ON MATCH SET a += row.props, a.updated_at = datetime()
"""

_GET_AUTHOR_CYPHER = """
MATCH (a:Author {id: $id, repository_id: $repository_id})
RETURN a
"""

_LIST_AUTHORS_CYPHER = """
MATCH (a:Author {repository_id: $repository_id})
RETURN a
"""


class AuthorRepository:
    def __init__(self, driver: Any) -> None:
        self._driver = driver

    def _validate(self, author: Author) -> None:
        if not author.id or not author.repository_id:
            raise GraphValidationError("Author requires id and repository_id")
        if not author.name and not author.email:
            raise GraphValidationError("Author requires name or email")

    def upsert(self, author: Author) -> Author:
        self._validate(author)
        props = {"name": author.name, "email": author.email}

        def _work(tx: Any) -> Any:
            result = tx.run(
                _UPSERT_AUTHOR_CYPHER,
                id=author.id,
                repository_id=author.repository_id,
                props=props,
            )
            try:
                result.consume()
            except AttributeError:
                pass
            return author

        return _execute_write(self._driver, _work)  # type: ignore[no-any-return]

    def upsert_batch(self, repository_id: str, authors: list[Author]) -> int:
        if not repository_id:
            raise GraphValidationError("repository_id must not be empty")
        if not authors:
            return 0
        for author in authors:
            self._validate(author)
            if author.repository_id != repository_id:
                raise GraphValidationError(
                    f"Author {author.id} belongs to {author.repository_id!r}, "
                    f"expected {repository_id!r}"
                )

        batch = [
            {
                "id": a.id,
                "repository_id": a.repository_id,
                "props": {"name": a.name, "email": a.email},
            }
            for a in authors
        ]
        batch_size = getattr(self._driver, "batch_size", 500)
        total = 0
        for chunk in _chunk_batch(batch, batch_size):

            def _work(tx: Any, _chunk: list[dict[str, Any]] = chunk) -> None:
                res = tx.run(_BATCH_UPSERT_AUTHOR_CYPHER, batch=_chunk)
                try:
                    res.consume()
                except AttributeError:
                    pass

            _execute_write(self._driver, _work)
            total += len(chunk)
        return total

    def get(self, author_id: str, repository_id: str) -> Author | None:
        if not author_id or not repository_id:
            raise GraphValidationError("author_id and repository_id must not be empty")

        def _work(tx: Any) -> Author | None:
            result = tx.run(_GET_AUTHOR_CYPHER, id=author_id, repository_id=repository_id)
            try:
                record = result.single()
            except AttributeError:
                data = result.data() if hasattr(result, "data") else []
                if not data:
                    return None
                first = data[0]
                return Author.from_record(first["a"] if "a" in first else first)
            if record is None:
                return None
            try:
                node = record["a"]
                props = dict(node) if hasattr(node, "keys") else node
                return Author.from_record(props if isinstance(props, dict) else dict(node))
            except Exception:
                try:
                    data = result.data()
                    if data:
                        return Author.from_record(data[0]["a"] if "a" in data[0] else data[0])
                except Exception:
                    pass
                return None

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except GraphValidationError:
            raise
        except Exception as e:
            raise map_neo4j_error(e) from e

    def list_by_repository(self, repository_id: str) -> list[Author]:
        if not repository_id:
            raise GraphValidationError("repository_id must not be empty")

        def _work(tx: Any) -> list[Author]:
            result = tx.run(_LIST_AUTHORS_CYPHER, repository_id=repository_id)
            try:
                data = result.data()
                out: list[Author] = []
                for row in data:
                    rec = row["a"] if "a" in row else row
                    # rec may be Node; convert
                    if hasattr(rec, "keys"):
                        rec = dict(rec)
                    out.append(Author.from_record(rec))
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