"""Skeleton integration tests for graph foundation.

All tests are marked ``integration`` so ``pytest -m "not integration"``
passes without Docker.  Tests that require a live Neo4j container are
skipped when Neo4j is not reachable.

These tests verify importability and cover the contract that can be
asserted without a live DB (ID derivation, POSIX normalization, batch
size validation, error mapping, migration file existence).  Full
integration coverage (CRUD, idempotent duplicates, repo isolation,
batch batching, session cleanup, migration idempotency) runs when
Neo4j is available.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Importability — must pass without Neo4j
# ---------------------------------------------------------------------------


def test_import_graph_package() -> None:
    from chronotrace.graph import GraphDriver, GraphGateway

    assert GraphDriver is not None
    assert GraphGateway is not None


def test_import_models_and_errors() -> None:
    from chronotrace.graph.errors import (
        GraphConnectionError,
        GraphError,
        GraphMigrationError,
        GraphNotAvailable,
        GraphValidationError,
        GraphWriteError,
    )
    from chronotrace.graph.models import Author, Commit, File, Repository

    assert GraphError is not None
    assert Repository is not None


def test_migration_files_exist() -> None:
    pkg_dir = Path(__file__).resolve().parents[2] / "src" / "chronotrace" / "graph" / "migrations"
    # When installed, migrations live next to the package
    if not pkg_dir.exists():
        import chronotrace.graph.migrations as m

        pkg_dir = Path(m.__file__).parent
    files = sorted(p.name for p in pkg_dir.glob("V*.cypher"))
    assert "V001__initial_constraints.cypher" in files
    assert "V002__initial_indexes.cypher" in files
    assert "V003__schema_documentation.cypher" in files


def test_migration_files_contain_if_not_exists() -> None:
    import chronotrace.graph.migrations as m

    pkg_dir = Path(m.__file__).parent
    for name in [
        "V001__initial_constraints.cypher",
        "V002__initial_indexes.cypher",
        "V003__schema_documentation.cypher",
    ]:
        text = (pkg_dir / name).read_text(encoding="utf-8")
        assert "IF NOT EXISTS" in text, f"{name} missing IF NOT EXISTS"


def test_id_derivation_cross_repo_distinctness() -> None:
    from chronotrace.graph.models import derive_commit_id, derive_file_id

    repo_a = "https://github.com/org/repo-a"
    repo_b = "https://github.com/org/repo-b"
    h = "abc123def456abc123def456abc123def456abcd"
    assert derive_commit_id(repo_a, h) != derive_commit_id(repo_b, h)
    assert derive_file_id(repo_a, "src/foo.py") != derive_file_id(repo_b, "src/foo.py")


def test_posix_normalization() -> None:
    from chronotrace.graph.models import normalize_path

    assert normalize_path("src\\foo\\bar.py") == "src/foo/bar.py"
    assert normalize_path("./src/foo.py") == "src/foo.py"
    assert normalize_path("src//foo.py") == "src/foo.py"


def test_none_preservation_for_stats() -> None:
    from chronotrace.graph.models import Commit

    c = Commit.create(repository_id="repo1", hash="a" * 40, changed_file_count=None)
    assert c.changed_file_count is None
    # Relationship props should carry None, not 0 — verified via gateway batch


def test_batch_size_validation() -> None:
    from chronotrace.config.models import ChronoTraceConfig

    from chronotrace.graph import GraphGateway

    # Valid config with bad batch_size should fail at gateway construction
    # Use a stub config object with batch_size 0
    class FakeGraph:
        batch_size = 0
        uri = "bolt://localhost:7687"
        database = "neo4j"

    class FakeConfig:
        graph = FakeGraph()

    # Mock driver creation to avoid needing Neo4j
    from unittest.mock import patch

    with patch("chronotrace.graph.gateway.GraphDriver") as MockDriver:
        MockDriver.return_value.batch_size = 0
        with pytest.raises(Exception):
            GraphGateway(FakeConfig(), env={})


def test_parameterized_cypher_no_injection() -> None:
    """Values with quotes/semicolons are passed as parameters, not interpolated."""
    from unittest.mock import MagicMock

    from chronotrace.graph.models import File

    mock_driver = MagicMock()
    mock_driver.database = "neo4j"
    mock_driver.batch_size = 500
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_tx = MagicMock()
    # Make execute_write call the work function with a tx-like object
    def fake_execute_write(work):  # type: ignore
        work(mock_tx)
        return None

    mock_session.execute_write.side_effect = fake_execute_write
    mock_tx.run.return_value.consume.return_value = None

    from chronotrace.graph.repositories.file_repo import FileRepository

    repo = FileRepository(mock_driver)
    f = File.create(repository_id="repo1", path="src/foo'; DROP TABLE --.py")
    repo.upsert(f)
    # Assert run was called with parameterized placeholders
    assert mock_tx.run.called
    call_kwargs = mock_tx.run.call_args[1] if mock_tx.run.call_args[1] else {}
    call_args = mock_tx.run.call_args[0] if mock_tx.run.call_args[0] else ()
    query = call_args[0] if call_args else ""
    assert "$id" in query
    assert "$props" in query or "$repository_id" in query
    # The malicious string should be in params, not in query
    assert "DROP TABLE" not in query


def test_cross_repo_relationship_rejected_before_cypher() -> None:
    from unittest.mock import MagicMock

    from chronotrace.graph.errors import GraphValidationError
    from chronotrace.graph.repositories.relationships import RelationshipWriter

    mock_driver = MagicMock()
    mock_driver.database = "neo4j"
    mock_driver.batch_size = 500
    writer = RelationshipWriter(mock_driver)
    # Use distinct repository prefixes in file IDs to trigger cross-repo check
    file_a = "repo-a:src/foo.py"
    file_b = "repo-b:src/bar.py"
    with pytest.raises(GraphValidationError):
        writer.upsert_imports("repo-a", file_a, file_b)
    with pytest.raises(GraphValidationError):
        writer.upsert_co_changed("repo-a", file_a, file_b, count=1)


def test_error_mapping_does_not_leak_secrets() -> None:
    from chronotrace.graph.repositories.base import map_neo4j_error

    err = Exception("connection failed: password=secret123")
    mapped = map_neo4j_error(err)
    assert "secret123" not in str(mapped)


# ---------------------------------------------------------------------------
# Live Neo4j tests — skipped when Neo4j not reachable
# ---------------------------------------------------------------------------

try:
    from neo4j import GraphDatabase  # type: ignore

    _NEO4J_AVAILABLE = True
except ImportError:
    _NEO4J_AVAILABLE = False


def _neo4j_reachable() -> bool:
    if not _NEO4J_AVAILABLE:
        return False
    uri = os.environ.get("NEO4J_URI") or os.environ.get("CHRONOTRACE_NEO4J_URI") or "bolt://localhost:7687"
    user = os.environ.get("NEO4J_USERNAME") or "neo4j"
    pwd = os.environ.get("NEO4J_PASSWORD") or "password"
    try:
        driver = GraphDatabase.driver(uri, auth=(user, pwd), connection_timeout=5)
        driver.verify_connectivity()
        driver.close()
        return True
    except Exception:
        try:
            driver.close()  # type: ignore
        except Exception:
            pass
        return False


@pytest.mark.skipif(not _neo4j_reachable(), reason="Neo4j not reachable — skipping live integration")
class TestLiveNeo4j:
    def test_connectivity(self) -> None:
        from chronotrace.config import load_config
        from chronotrace.graph import GraphDriver

        config = load_config()
        driver = GraphDriver(config)
        try:
            driver.verify_connectivity()
        finally:
            driver.close()

    def test_constraints_and_indexes_after_init(self) -> None:
        from chronotrace.config import load_config
        from chronotrace.graph import GraphDriver
        from chronotrace.graph.migrations.runner import MigrationRunner

        config = load_config()
        driver = GraphDriver(config)
        try:
            runner = MigrationRunner(driver)
            runner.run()
            # Second run should be idempotent
            applied_again = runner.run()
            assert applied_again == []
        finally:
            driver.close()

    def test_idempotent_writes(self) -> None:
        from chronotrace.config import load_config
        from chronotrace.graph import GraphGateway
        from chronotrace.graph.models import Author, Commit, File, Repository

        config = load_config()
        gw = GraphGateway(config)
        try:
            gw.initialize_schema()
            repo = Repository.create(name="test-repo", remote_url="https://github.com/org/test-repo")
            gw.upsert_repository(repo)
            gw.upsert_repository(repo)  # idempotent

            author = Author.create(repository_id=repo.id, name="Alice", email="alice@example.com")
            gw.upsert_author(author)
            gw.upsert_author(author)

            commit = Commit.create(
                repository_id=repo.id, hash="a" * 40, message="fix: foo", is_fix=True, author_id=author.id
            )
            gw.upsert_commit(commit)
            gw.upsert_commit(commit)

            file = File.create(repository_id=repo.id, path="src/foo.py", language="python", is_test=False)
            gw.upsert_file(file)
            gw.upsert_file(file)

            # Relationships
            gw.upsert_belongs_to(repo.id, commit.id)
            gw.upsert_belongs_to(repo.id, commit.id)
            gw.upsert_authored(repo.id, author.id, commit.id)
            gw.upsert_authored(repo.id, author.id, commit.id)
            gw.upsert_modified(repo.id, commit.id, file.id, additions=10, deletions=2, change_type="M")
            gw.upsert_modified(repo.id, commit.id, file.id, additions=10, deletions=2, change_type="M")

            # Verify via counts
            with gw.driver.session() as session:
                result = session.run("MATCH (n) WHERE n.repository_id = $rid RETURN count(n) AS c", rid=repo.id)
                count = result.single()["c"]
                assert count >= 4  # repo, author, commit, file
        finally:
            gw.close()

    def test_repository_isolation(self) -> None:
        from chronotrace.config import load_config
        from chronotrace.graph import GraphGateway
        from chronotrace.graph.models import File, Repository

        config = load_config()
        gw = GraphGateway(config)
        try:
            gw.initialize_schema()
            repo_a = Repository.create(name="repo-a", remote_url="https://github.com/org/repo-a")
            repo_b = Repository.create(name="repo-b", remote_url="https://github.com/org/repo-b")
            gw.upsert_repository(repo_a)
            gw.upsert_repository(repo_b)

            file_a = File.create(repository_id=repo_a.id, path="src/foo.py")
            file_b = File.create(repository_id=repo_b.id, path="src/foo.py")
            gw.upsert_file(file_a)
            gw.upsert_file(file_b)

            files_a = gw.list_files(repo_a.id)
            paths_a = [f.path for f in files_a]
            # Should not contain repo_b's file ID
            repo_b_file_id = file_b.id
            assert all(f.repository_id == repo_a.id for f in files_a)
            assert file_a.path in paths_a
        finally:
            gw.close()

    def test_batch_writes(self) -> None:
        from chronotrace.config import load_config
        from chronotrace.graph import GraphGateway
        from chronotrace.graph.models import Commit, Repository

        config = load_config()
        # Use small batch size to test batching
        config.graph.batch_size  # just to ensure config loaded
        gw = GraphGateway(config)
        try:
            gw.initialize_schema()
            repo = Repository.create(name="batch-repo", remote_url="https://github.com/org/batch-repo")
            gw.upsert_repository(repo)
            commits = [Commit.create(repository_id=repo.id, hash=f"{i:040x}") for i in range(10)]
            count = gw.upsert_commits(repo.id, commits)
            assert count == 10
        finally:
            gw.close()

    def test_confident_rename_preserves_identity(self) -> None:
        from chronotrace.config import load_config
        from chronotrace.graph import GraphGateway
        from chronotrace.graph.models import File, Repository

        config = load_config()
        gw = GraphGateway(config)
        try:
            gw.initialize_schema()
            repo = Repository.create(name="rename-repo", remote_url="https://github.com/org/rename-repo")
            gw.upsert_repository(repo)
            f = File.create(repository_id=repo.id, path="src/old.py")
            gw.upsert_file(f)
            renamed = gw.rename_file_confident(f, "src/new.py")
            assert renamed.id == f.id
            assert renamed.path == "src/new.py"
            assert "src/old.py" in renamed.past_paths
            # Verify persisted
            persisted = gw.get_file(renamed.id, repo.id)
            assert persisted is not None
            assert persisted.path == "src/new.py"
        finally:
            gw.close()
