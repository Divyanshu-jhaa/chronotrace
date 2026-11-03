"""File entity persistence with rename handling."""

from __future__ import annotations

from typing import Any

from ..errors import GraphValidationError
from ..models import File
from .base import _execute_write, map_neo4j_error

_UPSERT_FILE_CYPHER = """
MERGE (f:File {id: $id, repository_id: $repository_id})
ON CREATE SET f += $props, f.created_at = datetime()
ON MATCH SET f += $props, f.updated_at = datetime()
RETURN f
"""

_BATCH_UPSERT_FILE_CYPHER = """
UNWIND $batch AS row
MERGE (f:File {id: row.id, repository_id: row.repository_id})
ON CREATE SET f += row.props, f.created_at = datetime()
ON MATCH SET f += row.props, f.updated_at = datetime()
"""

_GET_FILE_CYPHER = """
MATCH (f:File {id: $id, repository_id: $repository_id})
RETURN f
"""

_LIST_FILES_CYPHER = """
MATCH (f:File {repository_id: $repository_id})
RETURN f
"""

_GET_BY_PATH_CYPHER = """
MATCH (f:File {repository_id: $repository_id, path: $path})
RETURN f
"""

# For rename: confident rename updates existing node, uncertain keeps distinct.
# We use MERGE on id but update path/current_path and past_paths.


class FileRepository:
    def __init__(self, driver: Any) -> None:
        self._driver = driver

    def _validate(self, file: File) -> None:
        if not file.id or not file.repository_id or not file.path:
            raise GraphValidationError("File requires id, repository_id, and path")

    def upsert(self, file: File) -> File:
        self._validate(file)
        props = {
            "path": file.path,
            "current_path": file.current_path,
            "past_paths": list(file.past_paths),
            "alive": file.alive,
            "is_test": file.is_test,
            "language": file.language,
        }

        def _work(tx: Any) -> Any:
            result = tx.run(
                _UPSERT_FILE_CYPHER,
                id=file.id,
                repository_id=file.repository_id,
                props=props,
            )
            try:
                result.consume()
            except AttributeError:
                pass
            return file

        return _execute_write(self._driver, _work)  # type: ignore[no-any-return]

    def upsert_batch(self, repository_id: str, files: list[File]) -> int:
        if not repository_id:
            raise GraphValidationError("repository_id must not be empty")
        if not files:
            return 0
        for f in files:
            self._validate(f)
            if f.repository_id != repository_id:
                raise GraphValidationError(
                    f"File {f.id} belongs to {f.repository_id!r}, expected {repository_id!r}"
                )

        batch = [
            {
                "id": f.id,
                "repository_id": f.repository_id,
                "props": {
                    "path": f.path,
                    "current_path": f.current_path,
                    "past_paths": list(f.past_paths),
                    "alive": f.alive,
                    "is_test": f.is_test,
                    "language": f.language,
                },
            }
            for f in files
        ]
        batch_size = getattr(self._driver, "batch_size", 500)
        total = 0
        for chunk in _chunk_batch(batch, batch_size):

            def _work(tx: Any, _chunk: list[dict[str, Any]] = chunk) -> None:
                res = tx.run(_BATCH_UPSERT_FILE_CYPHER, batch=_chunk)
                try:
                    res.consume()
                except AttributeError:
                    pass

            _execute_write(self._driver, _work)
            total += len(chunk)
        return total

    def get(self, file_id: str, repository_id: str) -> File | None:
        if not file_id or not repository_id:
            raise GraphValidationError("file_id and repository_id must not be empty")

        def _work(tx: Any) -> File | None:
            result = tx.run(_GET_FILE_CYPHER, id=file_id, repository_id=repository_id)
            try:
                record = result.single()
            except AttributeError:
                data = result.data() if hasattr(result, "data") else []
                if not data:
                    return None
                first = data[0]
                return File.from_record(first["f"] if "f" in first else first)
            if record is None:
                return None
            try:
                node = record["f"]
                props = dict(node) if hasattr(node, "keys") else node
                return File.from_record(props if isinstance(props, dict) else dict(node))
            except Exception:
                try:
                    data = result.data()
                    if data:
                        return File.from_record(data[0]["f"] if "f" in data[0] else data[0])
                except Exception:
                    pass
                return None

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except GraphValidationError:
            raise
        except Exception as e:
            raise map_neo4j_error(e) from e

    def get_by_path(self, repository_id: str, path: str) -> File | None:
        if not repository_id or not path:
            raise GraphValidationError("repository_id and path must not be empty")
        from ..models import normalize_path

        norm = normalize_path(path)

        def _work(tx: Any) -> File | None:
            result = tx.run(_GET_BY_PATH_CYPHER, repository_id=repository_id, path=norm)
            try:
                record = result.single()
            except AttributeError:
                data = result.data() if hasattr(result, "data") else []
                if not data:
                    return None
                first = data[0]
                return File.from_record(first["f"] if "f" in first else first)
            if record is None:
                return None
            try:
                node = record["f"]
                props = dict(node) if hasattr(node, "keys") else node
                return File.from_record(props if isinstance(props, dict) else dict(node))
            except Exception:
                return None

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except GraphValidationError:
            raise
        except Exception as e:
            raise map_neo4j_error(e) from e

    def list_by_repository(self, repository_id: str) -> list[File]:
        if not repository_id:
            raise GraphValidationError("repository_id must not be empty")

        def _work(tx: Any) -> list[File]:
            result = tx.run(_LIST_FILES_CYPHER, repository_id=repository_id)
            try:
                data = result.data()
                out: list[File] = []
                for row in data:
                    rec = row["f"] if "f" in row else row
                    if hasattr(rec, "keys"):
                        rec = dict(rec)
                    out.append(File.from_record(rec))
                return out
            except AttributeError:
                return []

        try:
            return _execute_write(self._driver, _work, is_read=True)  # type: ignore[no-any-return]
        except GraphValidationError:
            raise
        except Exception as e:
            raise map_neo4j_error(e) from e

    def rename_confident(self, existing_file: File, new_path: str) -> File:
        """Handle confident rename: update path/current_path, append old to past_paths.

        Preserves node identity (same id).  Caller must have established
        confidence via Git rename detection (iter-3 owns that decision).
        """
        updated = existing_file.with_rename(new_path, confident=True)
        return self.upsert(updated)

    # Uncertain rename: caller creates a new distinct File node via upsert


def _chunk_batch(batch: list[Any], size: int) -> Any:  # type: ignore[misc]
    for i in range(0, len(batch), size):
        yield batch[i : i + size]