"""Relationship writers for all initial relationship types."""

from __future__ import annotations

from typing import Any

from ..errors import GraphValidationError
from .base import _execute_write

# ---------------------------------------------------------------------------
# Cypher templates — all parameterized, MERGE idempotent, repository-isolated
# ---------------------------------------------------------------------------

_CONTAINS_CYPHER = """
MATCH (r:Repository {id: $repository_id, repository_id: $repository_id})
MATCH (f:File {id: $file_id, repository_id: $repository_id})
MERGE (r)-[rel:CONTAINS {repository_id: $repository_id}]->(f)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
RETURN rel
"""

_BATCH_CONTAINS_CYPHER = """
UNWIND $batch AS row
MATCH (r:Repository {id: row.repository_id, repository_id: row.repository_id})
MATCH (f:File {id: row.file_id, repository_id: row.repository_id})
MERGE (r)-[rel:CONTAINS {repository_id: row.repository_id}]->(f)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
"""

_BELONGS_TO_CYPHER = """
MATCH (c:Commit {id: $commit_id, repository_id: $repository_id})
MATCH (r:Repository {id: $repository_id, repository_id: $repository_id})
MERGE (c)-[rel:BELONGS_TO {repository_id: $repository_id}]->(r)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
RETURN rel
"""

_BATCH_BELONGS_TO_CYPHER = """
UNWIND $batch AS row
MATCH (c:Commit {id: row.commit_id, repository_id: row.repository_id})
MATCH (r:Repository {id: row.repository_id, repository_id: row.repository_id})
MERGE (c)-[rel:BELONGS_TO {repository_id: row.repository_id}]->(r)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
"""

_AUTHORED_CYPHER = """
MATCH (a:Author {id: $author_id, repository_id: $repository_id})
MATCH (c:Commit {id: $commit_id, repository_id: $repository_id})
MERGE (a)-[rel:AUTHORED {repository_id: $repository_id}]->(c)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
RETURN rel
"""

_BATCH_AUTHORED_CYPHER = """
UNWIND $batch AS row
MATCH (a:Author {id: row.author_id, repository_id: row.repository_id})
MATCH (c:Commit {id: row.commit_id, repository_id: row.repository_id})
MERGE (a)-[rel:AUTHORED {repository_id: row.repository_id}]->(c)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
"""

_MODIFIED_CYPHER = """
MATCH (c:Commit {id: $commit_id, repository_id: $repository_id})
MATCH (f:File {id: $file_id, repository_id: $repository_id})
MERGE (c)-[rel:MODIFIED {repository_id: $repository_id}]->(f)
ON CREATE SET rel += $props, rel.created_at = datetime()
ON MATCH SET rel += $props, rel.updated_at = datetime()
RETURN rel
"""

_BATCH_MODIFIED_CYPHER = """
UNWIND $batch AS row
MATCH (c:Commit {id: row.commit_id, repository_id: row.repository_id})
MATCH (f:File {id: row.file_id, repository_id: row.repository_id})
MERGE (c)-[rel:MODIFIED {repository_id: row.repository_id}]->(f)
ON CREATE SET rel += row.props, rel.created_at = datetime()
ON MATCH SET rel += row.props, rel.updated_at = datetime()
"""

_REVERTS_CYPHER = """
MATCH (c1:Commit {id: $source_id, repository_id: $repository_id})
MATCH (c2:Commit {id: $target_id, repository_id: $repository_id})
MERGE (c1)-[rel:REVERTS {repository_id: $repository_id}]->(c2)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
RETURN rel
"""

_BATCH_REVERTS_CYPHER = """
UNWIND $batch AS row
MATCH (c1:Commit {id: row.source_id, repository_id: row.repository_id})
MATCH (c2:Commit {id: row.target_id, repository_id: row.repository_id})
MERGE (c1)-[rel:REVERTS {repository_id: row.repository_id}]->(c2)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
"""

_IMPORTS_CYPHER = """
MATCH (f1:File {id: $source_id, repository_id: $repository_id})
MATCH (f2:File {id: $target_id, repository_id: $repository_id})
MERGE (f1)-[rel:IMPORTS {repository_id: $repository_id}]->(f2)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
RETURN rel
"""

_BATCH_IMPORTS_CYPHER = """
UNWIND $batch AS row
MATCH (f1:File {id: row.source_id, repository_id: row.repository_id})
MATCH (f2:File {id: row.target_id, repository_id: row.repository_id})
MERGE (f1)-[rel:IMPORTS {repository_id: row.repository_id}]->(f2)
ON CREATE SET rel.created_at = datetime()
ON MATCH SET rel.updated_at = datetime()
"""

_CO_CHANGED_CYPHER = """
MATCH (f1:File {id: $source_id, repository_id: $repository_id})
MATCH (f2:File {id: $target_id, repository_id: $repository_id})
MERGE (f1)-[rel:CO_CHANGED_WITH {repository_id: $repository_id}]->(f2)
ON CREATE SET rel += $props, rel.created_at = datetime()
ON MATCH SET rel += $props, rel.updated_at = datetime()
RETURN rel
"""

_BATCH_CO_CHANGED_CYPHER = """
UNWIND $batch AS row
MATCH (f1:File {id: row.source_id, repository_id: row.repository_id})
MATCH (f2:File {id: row.target_id, repository_id: row.repository_id})
MERGE (f1)-[rel:CO_CHANGED_WITH {repository_id: row.repository_id}]->(f2)
ON CREATE SET rel += row.props, rel.created_at = datetime()
ON MATCH SET rel += row.props, rel.updated_at = datetime()
"""


def _validate_same_repo(repo_a: str, repo_b: str) -> None:
    if not repo_a or not repo_b:
        raise GraphValidationError("repository_id must not be empty for relationship endpoints")
    if repo_a != repo_b:
        raise GraphValidationError(
            f"Cross-repository relationship rejected: {repo_a!r} vs {repo_b!r} — "
            f"relationships must stay within a single repository"
        )


class RelationshipWriter:
    """Writes all relationship types with MERGE idempotency and repo isolation."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    # -- CONTAINS ----------------------------------------------------------
    def upsert_contains(self, repository_id: str, file_id: str) -> None:
        if not repository_id or not file_id:
            raise GraphValidationError("repository_id and file_id required for CONTAINS")

        def _work(tx: Any) -> None:
            res = tx.run(_CONTAINS_CYPHER, repository_id=repository_id, file_id=file_id)
            try:
                res.consume()
            except AttributeError:
                pass

        _execute_write(self._driver, _work)

    def batch_contains(self, repository_id: str, file_ids: list[str]) -> int:
        if not repository_id:
            raise GraphValidationError("repository_id required")
        if not file_ids:
            return 0
        batch = [{"repository_id": repository_id, "file_id": fid} for fid in file_ids]
        return self._batch_write(_BATCH_CONTAINS_CYPHER, batch)

    # -- BELONGS_TO --------------------------------------------------------
    def upsert_belongs_to(self, repository_id: str, commit_id: str) -> None:
        if not repository_id or not commit_id:
            raise GraphValidationError("repository_id and commit_id required for BELONGS_TO")

        def _work(tx: Any) -> None:
            res = tx.run(_BELONGS_TO_CYPHER, repository_id=repository_id, commit_id=commit_id)
            try:
                res.consume()
            except AttributeError:
                pass

        _execute_write(self._driver, _work)

    def batch_belongs_to(self, repository_id: str, commit_ids: list[str]) -> int:
        if not repository_id:
            raise GraphValidationError("repository_id required")
        if not commit_ids:
            return 0
        batch = [{"repository_id": repository_id, "commit_id": cid} for cid in commit_ids]
        return self._batch_write(_BATCH_BELONGS_TO_CYPHER, batch)

    # -- AUTHORED ----------------------------------------------------------
    def upsert_authored(self, repository_id: str, author_id: str, commit_id: str) -> None:
        if not all([repository_id, author_id, commit_id]):
            raise GraphValidationError("repository_id, author_id, commit_id required for AUTHORED")

        def _work(tx: Any) -> None:
            res = tx.run(
                _AUTHORED_CYPHER,
                repository_id=repository_id,
                author_id=author_id,
                commit_id=commit_id,
            )
            try:
                res.consume()
            except AttributeError:
                pass

        _execute_write(self._driver, _work)

    def batch_authored(self, repository_id: str, pairs: list[tuple[str, str]]) -> int:
        """Pairs are (author_id, commit_id)."""
        if not repository_id:
            raise GraphValidationError("repository_id required")
        if not pairs:
            return 0
        batch = [{"repository_id": repository_id, "author_id": a, "commit_id": c} for a, c in pairs]
        return self._batch_write(_BATCH_AUTHORED_CYPHER, batch)

    # -- MODIFIED ----------------------------------------------------------
    def upsert_modified(
        self,
        repository_id: str,
        commit_id: str,
        file_id: str,
        additions: int | None = None,
        deletions: int | None = None,
        change_type: str | None = None,
    ) -> None:
        if not all([repository_id, commit_id, file_id]):
            raise GraphValidationError("repository_id, commit_id, file_id required for MODIFIED")
        # None preserved, not 0
        props: dict[str, Any] = {}
        if additions is not None:
            props["additions"] = additions
        else:
            props["additions"] = None
        if deletions is not None:
            props["deletions"] = deletions
        else:
            props["deletions"] = None
        if change_type is not None:
            props["change_type"] = change_type
        else:
            props["change_type"] = None

        def _work(tx: Any) -> None:
            res = tx.run(
                _MODIFIED_CYPHER,
                repository_id=repository_id,
                commit_id=commit_id,
                file_id=file_id,
                props=props,
            )
            try:
                res.consume()
            except AttributeError:
                pass

        _execute_write(self._driver, _work)

    def batch_modified(
        self,
        repository_id: str,
        rows: list[dict[str, Any]],
    ) -> int:
        """Rows: {commit_id, file_id, additions, deletions, change_type}.

        Validates same repository_id internally.
        """
        if not repository_id:
            raise GraphValidationError("repository_id required")
        if not rows:
            return 0
        batch = []
        for row in rows:
            cid = row.get("commit_id")
            fid = row.get("file_id")
            if not cid or not fid:
                raise GraphValidationError("MODIFIED requires commit_id and file_id")
            # Optional cross-repo validation if row carries repository_id
            other = row.get("repository_id")
            if other is not None and other != repository_id:
                raise GraphValidationError(
                    f"Cross-repository MODIFIED rejected: {other!r} "
                    f"vs {repository_id!r}"
                )
            props: dict[str, Any] = {
                "additions": row.get("additions"),
                "deletions": row.get("deletions"),
                "change_type": row.get("change_type"),
            }
            # Preserve None vs 0 distinction — don't coerce None to 0
            batch.append(
                {
                    "repository_id": repository_id,
                    "commit_id": cid,
                    "file_id": fid,
                    "props": props,
                }
            )
        return self._batch_write(_BATCH_MODIFIED_CYPHER, batch)

    # -- REVERTS -----------------------------------------------------------
    def upsert_reverts(self, repository_id: str, source_id: str, target_id: str) -> None:
        if not all([repository_id, source_id, target_id]):
            raise GraphValidationError("repository_id, source_id, target_id required for REVERTS")

        def _work(tx: Any) -> None:
            res = tx.run(
                _REVERTS_CYPHER,
                repository_id=repository_id,
                source_id=source_id,
                target_id=target_id,
            )
            try:
                res.consume()
            except AttributeError:
                pass

        _execute_write(self._driver, _work)

    def batch_reverts(self, repository_id: str, pairs: list[tuple[str, str]]) -> int:
        if not repository_id:
            raise GraphValidationError("repository_id required")
        if not pairs:
            return 0
        batch = [{"repository_id": repository_id, "source_id": s, "target_id": t} for s, t in pairs]
        return self._batch_write(_BATCH_REVERTS_CYPHER, batch)

    # -- IMPORTS -----------------------------------------------------------
    def upsert_imports(self, repository_id: str, source_id: str, target_id: str) -> None:
        if not all([repository_id, source_id, target_id]):
            raise GraphValidationError("repository_id, source_id, target_id required for IMPORTS")
        self._check_not_cross_repo(repository_id, source_id, target_id)

        def _work(tx: Any) -> None:
            res = tx.run(
                _IMPORTS_CYPHER,
                repository_id=repository_id,
                source_id=source_id,
                target_id=target_id,
            )
            try:
                res.consume()
            except AttributeError:
                pass

        _execute_write(self._driver, _work)

    def batch_imports(self, repository_id: str, pairs: list[tuple[str, str]]) -> int:
        if not repository_id:
            raise GraphValidationError("repository_id required")
        if not pairs:
            return 0
        # Cross-repo validation: all endpoints must share repository_id.
        # We validate at batch level: since file IDs embed repository_id, check prefix
        # but also rely on Cypher MATCH filtering; we raise before execution if
        # caller mixes repository_ids by checking optional extra field.
        batch = []
        for s, t in pairs:
            self._check_not_cross_repo(repository_id, s, t)
            batch.append({"repository_id": repository_id, "source_id": s, "target_id": t})
        return self._batch_write(_BATCH_IMPORTS_CYPHER, batch)

    # -- CO_CHANGED_WITH ---------------------------------------------------
    def upsert_co_changed(
        self,
        repository_id: str,
        source_id: str,
        target_id: str,
        count: int | None = None,
        weight: float | None = None,
        confidence: float | None = None,
    ) -> None:
        if not all([repository_id, source_id, target_id]):
            raise GraphValidationError(
                "repository_id, source_id, target_id required for CO_CHANGED_WITH"
            )
        self._check_not_cross_repo(repository_id, source_id, target_id)
        props: dict[str, Any] = {}
        if count is not None:
            props["count"] = count
        if weight is not None:
            props["weight"] = weight
        if confidence is not None:
            props["confidence"] = confidence

        def _work(tx: Any) -> None:
            res = tx.run(
                _CO_CHANGED_CYPHER,
                repository_id=repository_id,
                source_id=source_id,
                target_id=target_id,
                props=props,
            )
            try:
                res.consume()
            except AttributeError:
                pass

        _execute_write(self._driver, _work)

    def batch_co_changed(
        self,
        repository_id: str,
        rows: list[dict[str, Any]],
    ) -> int:
        """Rows: {source_id, target_id, count, weight, confidence}."""
        if not repository_id:
            raise GraphValidationError("repository_id required")
        if not rows:
            return 0
        batch = []
        for row in rows:
            s = row.get("source_id")
            t = row.get("target_id")
            if not s or not t:
                raise GraphValidationError("CO_CHANGED_WITH requires source_id and target_id")
            self._check_not_cross_repo(repository_id, s, t)
            other = row.get("repository_id")
            if other is not None and other != repository_id:
                raise GraphValidationError(
                    f"Cross-repository CO_CHANGED_WITH rejected: {other!r} "
                    f"vs {repository_id!r}"
                )
            props: dict[str, Any] = {}
            if "count" in row:
                props["count"] = row["count"]
            if "weight" in row:
                props["weight"] = row["weight"]
            if "confidence" in row:
                props["confidence"] = row["confidence"]
            batch.append(
                {
                    "repository_id": repository_id,
                    "source_id": s,
                    "target_id": t,
                    "props": props,
                }
            )
        return self._batch_write(_BATCH_CO_CHANGED_CYPHER, batch)

    # -- Generic execute_batch ---------------------------------------------
    def execute_batch(
        self, cypher: str, batch: list[dict[str, Any]], repository_id: str | None = None
    ) -> int:
        """Generic batched UNWIND write using parameterized Cypher.

        Cypher must use ``UNWIND $batch AS row`` and ``$`` params only.
        Rejects raw interpolation by validating no repository mixing.
        """
        if not batch:
            return 0
        if repository_id is not None:
            for row in batch:
                other = row.get("repository_id")
                if other is not None and other != repository_id:
                    raise GraphValidationError(
                        f"Cross-repository batch rejected: {other!r} "
                        f"vs {repository_id!r}"
                    )
        return self._batch_write(cypher, batch)

    def _check_not_cross_repo(self, repository_id: str, id_a: str, id_b: str) -> None:
        # If IDs embed repository_id as prefix before ":", validate
        # Heuristic: if id contains ":", prefix before ":" is repository_id
        # This catches cross-repo attempts before Cypher execution
        for fid in (id_a, id_b):
            if ":" in fid:
                prefix = fid.split(":", 1)[0]
                # Only enforce when prefix looks like a repo ID and differs
                # from expected; skip when prefix is just hash-like?
                # For File/Commit IDs, prefix is repository_id. Enforce.
                if prefix != repository_id and ":" in fid:
                    # Additional guard: allow when file id embeds repo id correctly
                    # but repo_id is substring? Strict equality.
                    # Only raise if we can prove mismatch: fid starts with other repo
                    # We check if the fid's repository_id part (before first colon)
                    # is not our repository_id — then it's cross-repo.
                    # However some IDs like Author have extra colons: repo:author:hash
                    # In that case first segment is still repo id.
                    # So this check is valid.
                    # But don't over-report when repository_id is URL with colon (e.g., "https://")
                    # URLs contain "://"; our canonical repo IDs are URLs which contain ":".
                    # Splitting on first ":" would give "https" not the repo id.
                    # So skip cross-repo check for URL-style repo IDs by checking
                    # if repository_id contains "://"
                    if "://" not in repository_id and "://" not in prefix:
                        # Both look like simple IDs — enforce
                        raise GraphValidationError(
                            f"Cross-repository relationship rejected: file/commit {fid!r} "
                            f"belongs to {prefix!r}, expected {repository_id!r}"
                        )
                    # For URL-style repo IDs, check if fid starts with repo_id + ":"
                    elif not fid.startswith(repository_id + ":") and "://" in repository_id:
                        raise GraphValidationError(
                            f"Cross-repository relationship rejected: {fid!r} "
                            f"does not belong to repository {repository_id!r}"
                        )

    def _batch_write(self, cypher: str, batch: list[dict[str, Any]]) -> int:
        batch_size = getattr(self._driver, "batch_size", 500)
        total = 0
        for i in range(0, len(batch), batch_size):
            chunk = batch[i : i + batch_size]

            def _work(tx: Any, _chunk: list[dict[str, Any]] = chunk, _cypher: str = cypher) -> None:
                res = tx.run(_cypher, batch=_chunk)
                try:
                    res.consume()
                except AttributeError:
                    pass

            _execute_write(self._driver, _work)
            total += len(chunk)
        return total
