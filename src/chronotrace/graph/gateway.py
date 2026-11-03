"""GraphGateway facade composing repositories."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .driver import GraphDriver
from .errors import GraphValidationError
from .migrations.runner import MigrationRunner
from .models import Author, Commit, File, Repository
from .repositories.author_repo import AuthorRepository
from .repositories.commit_repo import CommitRepository
from .repositories.file_repo import FileRepository
from .repositories.relationships import RelationshipWriter
from .repositories.repository_repo import RepositoryRepository


class GraphGateway:
    """Narrow public entry point for ingestion/analytics.

    Composes per-entity repositories and relationship writers.
    Keeps Cypher confined to repositories; upper layers import only
    this gateway and ``errors``.
    """

    def __init__(
        self,
        config: Any,
        env: Mapping[str, str] | None = None,
        driver: GraphDriver | None = None,
    ) -> None:
        # Validate batch_size at gateway construction (maps to config)
        raw_batch = None
        if hasattr(config, "graph") and hasattr(config.graph, "batch_size"):
            raw_batch = config.graph.batch_size
        elif hasattr(config, "batch_size"):
            raw_batch = config.batch_size
        if raw_batch is not None:
            try:
                b = int(raw_batch)
            except Exception as exc:
                raise GraphValidationError(
                    f"batch_size must be an integer, got {raw_batch!r}"
                ) from exc
            if b < 1 or b > 10000:
                raise GraphValidationError(f"batch_size must be between 1 and 10000, got {b}")

        self._config = config
        self._env: Mapping[str, str] = env if env is not None else {}

        if driver is not None:
            self._driver = driver
            self._owns_driver = False
        else:
            self._driver = GraphDriver(config, env=self._env)
            self._owns_driver = True

        # Compose repositories
        self.repositories = RepositoryRepository(self._driver)
        self.authors = AuthorRepository(self._driver)
        self.commits = CommitRepository(self._driver)
        self.files = FileRepository(self._driver)
        self.relationships = RelationshipWriter(self._driver)

        # Backward-compatible aliases per spec naming
        self.repository_repo = self.repositories
        self.author_repo = self.authors
        self.commit_repo = self.commits
        self.file_repo = self.files
        self.relationship_writer = self.relationships

    # -- Lifecycle ---------------------------------------------------------
    @property
    def driver(self) -> GraphDriver:
        return self._driver

    @property
    def batch_size(self) -> int:
        return self._driver.batch_size

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()

    def close(self) -> None:
        if self._owns_driver:
            self._driver.close()

    def __enter__(self) -> GraphGateway:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -- Schema ------------------------------------------------------------
    def initialize_schema(self, migrations_dir: Path | None = None) -> list[str]:
        runner = MigrationRunner(self._driver, migrations_dir=migrations_dir)
        return runner.run()

    def get_applied_migrations(self) -> dict[str, str]:
        runner = MigrationRunner(self._driver)
        return runner.get_applied_versions()

    # -- Repository --------------------------------------------------------
    def get_repository(self, repository_id: str) -> Repository | None:
        return self.repositories.get(repository_id)

    def upsert_repository(self, repository: Repository) -> Repository:
        return self.repositories.upsert(repository)

    def upsert_repositories(self, repositories: list[Repository]) -> int:
        return self.repositories.upsert_batch(repositories)

    # -- Author ------------------------------------------------------------
    def get_author(self, author_id: str, repository_id: str) -> Author | None:
        return self.authors.get(author_id, repository_id)

    def upsert_author(self, author: Author) -> Author:
        return self.authors.upsert(author)

    def upsert_authors(self, repository_id: str, authors: list[Author]) -> int:
        return self.authors.upsert_batch(repository_id, authors)

    def list_authors(self, repository_id: str) -> list[Author]:
        return self.authors.list_by_repository(repository_id)

    # -- Commit ------------------------------------------------------------
    def get_commit(self, commit_id: str, repository_id: str) -> Commit | None:
        return self.commits.get(commit_id, repository_id)

    def get_commit_by_hash(self, repository_id: str, commit_hash: str) -> Commit | None:
        return self.commits.get_by_hash(repository_id, commit_hash)

    def upsert_commit(self, commit: Commit) -> Commit:
        return self.commits.upsert(commit)

    def upsert_commits(self, repository_id: str, commits: list[Commit]) -> int:
        return self.commits.upsert_batch(repository_id, commits)

    def list_commits(self, repository_id: str) -> list[Commit]:
        return self.commits.list_by_repository(repository_id)

    # -- File --------------------------------------------------------------
    def get_file(self, file_id: str, repository_id: str) -> File | None:
        return self.files.get(file_id, repository_id)

    def get_file_by_path(self, repository_id: str, path: str) -> File | None:
        return self.files.get_by_path(repository_id, path)

    def upsert_file(self, file: File) -> File:
        return self.files.upsert(file)

    def upsert_files(self, repository_id: str, files: list[File]) -> int:
        return self.files.upsert_batch(repository_id, files)

    def list_files(self, repository_id: str) -> list[File]:
        return self.files.list_by_repository(repository_id)

    def rename_file_confident(self, existing_file: File, new_path: str) -> File:
        return self.files.rename_confident(existing_file, new_path)

    # -- Relationships -----------------------------------------------------
    # Thin pass-throughs so callers never import RelationshipWriter directly

    def upsert_contains(self, repository_id: str, file_id: str) -> None:
        self.relationships.upsert_contains(repository_id, file_id)

    def upsert_belongs_to(self, repository_id: str, commit_id: str) -> None:
        self.relationships.upsert_belongs_to(repository_id, commit_id)

    def upsert_authored(self, repository_id: str, author_id: str, commit_id: str) -> None:
        self.relationships.upsert_authored(repository_id, author_id, commit_id)

    def upsert_modified(
        self,
        repository_id: str,
        commit_id: str,
        file_id: str,
        additions: int | None = None,
        deletions: int | None = None,
        change_type: str | None = None,
    ) -> None:
        self.relationships.upsert_modified(
            repository_id, commit_id, file_id, additions, deletions, change_type
        )

    def upsert_reverts(self, repository_id: str, source_id: str, target_id: str) -> None:
        self.relationships.upsert_reverts(repository_id, source_id, target_id)

    def upsert_imports(self, repository_id: str, source_id: str, target_id: str) -> None:
        self.relationships.upsert_imports(repository_id, source_id, target_id)

    def upsert_co_changed(
        self,
        repository_id: str,
        source_id: str,
        target_id: str,
        count: int | None = None,
        weight: float | None = None,
        confidence: float | None = None,
    ) -> None:
        self.relationships.upsert_co_changed(
            repository_id, source_id, target_id, count, weight, confidence
        )

    # Batch relationship helpers
    def upsert_relationships(
        self, kind: str, repository_id: str, rows: list[dict[str, Any]]
    ) -> int:
        """Generic batch upsert dispatched by kind.

        kind: one of CONTAINS, BELONGS_TO, AUTHORED, MODIFIED,
        REVERTS, IMPORTS, CO_CHANGED_WITH
        """
        kind_upper = kind.upper()
        if kind_upper == "CONTAINS":
            # rows: list of file_ids or dicts with file_id
            file_ids: list[str] = []
            for r in rows:
                if isinstance(r, str):
                    file_ids.append(r)
                elif isinstance(r, dict):
                    fid = r.get("file_id") or r.get("id")
                    if fid:
                        file_ids.append(str(fid))
            return self.relationships.batch_contains(repository_id, file_ids)
        elif kind_upper == "BELONGS_TO":
            commit_ids: list[str] = []
            for r in rows:
                if isinstance(r, str):
                    commit_ids.append(r)
                elif isinstance(r, dict):
                    cid = r.get("commit_id") or r.get("id")
                    if cid:
                        commit_ids.append(str(cid))
            return self.relationships.batch_belongs_to(repository_id, commit_ids)
        elif kind_upper == "AUTHORED":
            pairs: list[tuple[str, str]] = []
            for r in rows:
                if isinstance(r, (list, tuple)) and len(r) == 2:
                    pairs.append((str(r[0]), str(r[1])))
                elif isinstance(r, dict):
                    pairs.append((str(r.get("author_id")), str(r.get("commit_id"))))
            return self.relationships.batch_authored(repository_id, pairs)
        elif kind_upper == "MODIFIED":
            return self.relationships.batch_modified(repository_id, rows)
        elif kind_upper == "REVERTS":
            pairs = []
            for r in rows:
                if isinstance(r, (list, tuple)) and len(r) == 2:
                    pairs.append((str(r[0]), str(r[1])))
                elif isinstance(r, dict):
                    pairs.append((str(r.get("source_id")), str(r.get("target_id"))))
            return self.relationships.batch_reverts(repository_id, pairs)
        elif kind_upper == "IMPORTS":
            pairs = []
            for r in rows:
                if isinstance(r, (list, tuple)) and len(r) == 2:
                    pairs.append((str(r[0]), str(r[1])))
                elif isinstance(r, dict):
                    pairs.append((str(r.get("source_id")), str(r.get("target_id"))))
            return self.relationships.batch_imports(repository_id, pairs)
        elif kind_upper == "CO_CHANGED_WITH":
            return self.relationships.batch_co_changed(repository_id, rows)
        else:
            raise GraphValidationError(f"Unknown relationship kind: {kind!r}")

    # -- Generic batch -----------------------------------------------------
    def execute_batch(
        self, cypher: str, batch: list[dict[str, Any]], repository_id: str | None = None
    ) -> int:
        return self.relationships.execute_batch(cypher, batch, repository_id)


# FastAPI wiring stub — for lifecycle integration
def create_gateway(config: Any, env: Mapping[str, str] | None = None) -> GraphGateway:
    """Factory for FastAPI dependency injection.

    Usage::

        gateway = create_gateway(config)
        # in FastAPI lifespan:
        # gateway.verify_connectivity()
        # yield
        # gateway.close()
    """
    return GraphGateway(config, env=env)


def get_gateway_dependency(config: Any) -> Any:
    """FastAPI dependency that yields a GraphGateway.

    Caller should provide the validated config object.
    This is a stub wiring point for iter-3+/api integration.
    """

    def _dep() -> Any:
        gateway = GraphGateway(config)
        try:
            yield gateway
        finally:
            gateway.close()

    return _dep
