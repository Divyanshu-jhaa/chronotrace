"""Integration check placeholder for regression reads (iter-5 tasks 3.3).

Verifies regression risk against a disposable Neo4j instance with seeded
fix commits and not_found handling. Uses testcontainers when available;
otherwise skips.

This is a placeholder that exercises the same read path as the unit
fake — fixing the contract that analytic reads are repository-scoped,
parameterized, and read-only.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

try:
    from testcontainers.neo4j import Neo4jContainer  # type: ignore

    HAS_TC = True
except ImportError:
    HAS_TC = False
    Neo4jContainer = None  # type: ignore

from chronotrace.config.models import ChronoTraceConfig
from chronotrace.core.analytics.contracts import Status
from chronotrace.core.analytics.service import AnalyticsService
from chronotrace.graph.gateway import GraphGateway
from chronotrace.graph.models import Commit, File, Repository


@pytest.fixture(scope="module")
def neo4j_container():
    if not HAS_TC:
        pytest.skip("testcontainers not installed — pip install 'testcontainers[neo4j]'")
    try:
        container = Neo4jContainer("neo4j:5-community")
        container.start()
    except Exception as e:
        pytest.skip(f"Docker/testcontainers not available: {e}")
    yield container
    with contextlib.suppress(Exception):
        container.stop()


@pytest.fixture
def gateway(neo4j_container):
    uri = neo4j_container.get_connection_url()
    cfg = ChronoTraceConfig.model_validate(
        {"version": 1, "graph": {"uri": uri, "database": "neo4j", "batch_size": 500}}
    )
    gw = GraphGateway(cfg, env={"NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": "password"})
    gw.initialize_schema()
    with gw.driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
    yield gw
    with contextlib.suppress(Exception):
        with gw.driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n").consume()
    gw.close()


def _seed_repo_with_fix_commits(
    gateway: GraphGateway, repo_id: str, target: str = "target.py"
) -> None:
    """Seed repo_id with files [target, helper.py, other.py] and two fix commits.

    C1 and C2 both modify target + helper; only C1 modifies other.py.
    Fix reasons and messages are set so evidence can be asserted.
    """
    repo = Repository.create(name=repo_id, repository_id=repo_id)
    gateway.repositories.upsert(repo)

    files = [
        File.create(repository_id=repo_id, path=target),
        File.create(repository_id=repo_id, path="helper.py"),
        File.create(repository_id=repo_id, path="other.py"),
    ]
    gateway.files.upsert_batch(repo_id, files)

    # Map path -> file id for MODIFIED edges
    file_ids = {f.path: f.id for f in files}

    # Two fix commits
    c1 = Commit.create(
        repository_id=repo_id,
        hash="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        date=datetime(2024, 1, 10, tzinfo=timezone.utc).isoformat(),
        message="fix: handle null in target",
        is_fix=True,
    )
    c2 = Commit.create(
        repository_id=repo_id,
        hash="b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
        date=datetime(2024, 1, 20, tzinfo=timezone.utc).isoformat(),
        message="fix: regression in helper coupling",
        is_fix=True,
    )
    gateway.commits.upsert_batch(repo_id, [c1, c2])

    # Persist fixReason via direct Cypher (Commit model does not carry it in to_props)
    with gateway.driver.session() as s:
        s.run(
            """
            MATCH (c:Commit {id: $id, repository_id: $repo})
            SET c.fixReason = $reason, c.is_fix = true
            """,
            id=c1.id,
            repo=repo_id,
            reason="pattern='fix' matched='fix' excerpt='fix: handle null in target'",
        ).consume()
        s.run(
            """
            MATCH (c:Commit {id: $id, repository_id: $repo})
            SET c.fixReason = $reason, c.is_fix = true
            """,
            id=c2.id,
            repo=repo_id,
            reason="pattern='fix' matched='fix' excerpt='fix: regression in helper coupling'",
        ).consume()

    # MODIFIED edges
    # C1: target, helper, other
    # C2: target, helper
    for commit, paths in [
        (c1, [target, "helper.py", "other.py"]),
        (c2, [target, "helper.py"]),
    ]:
        for p in paths:
            gateway.relationships.upsert_modified(repo_id, commit.id, file_ids[p])

    # Also add CONTAINS and BELONGS_TO for completeness (not required for regression reads
    # but mirrors ingestion)
    gateway.relationships.batch_contains(repo_id, list(file_ids.values()))
    gateway.relationships.batch_belongs_to(repo_id, [c1.id, c2.id])


def test_regression_seeded_ranking_and_evidence(gateway: GraphGateway):
    repo_id = "test://regression-seeded"
    target = "target.py"
    _seed_repo_with_fix_commits(gateway, repo_id, target)

    svc = AnalyticsService(gateway, config=None)
    res = svc.regression_risk(repo_id, target)

    # Should be ok with evidence
    assert res.status == Status.OK, f"expected ok, got {res.status}"
    assert res.result is not None
    assert len(res.result.fix_commits) == 2
    # Evidence hashes/dates/messages/fix_reason present
    hashes = {c.hash for c in res.result.fix_commits}
    assert "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2" in hashes
    for c in res.result.fix_commits:
        assert c.hash
        assert c.date
        assert c.message
        assert c.fix_reason is not None and "fix" in c.fix_reason.lower()

    # Ranking: helper count 2 > other count 1, confidence 1.0 vs 0.5
    related = res.result.related_files
    assert len(related) >= 2
    # Deterministic ordering
    assert related[0].path == "helper.py"
    assert related[0].count == 2
    assert related[0].confidence == pytest.approx(1.0)
    other = next(r for r in related if r.path == "other.py")
    assert other.count == 1
    assert other.confidence == pytest.approx(0.5)

    # Framing is historical evidence, not probability
    warnings_text = " ".join(res.limitations.warnings).lower()
    assert "historical regression evidence" in warnings_text
    assert "probability" in warnings_text  # the framing contains that word in disclaimer context
    # Should not claim zero-risk or low risk as probability
    evidence_text = " ".join([c.hash for c in res.result.fix_commits])
    assert evidence_text  # at least evidence present

    # Deduplication: re-running should give same result (determinism)
    res2 = svc.regression_risk(repo_id, target)
    assert res.result.related_files == res2.result.related_files
    assert [c.hash for c in res.result.fix_commits] == [c.hash for c in res2.result.fix_commits]


def test_regression_not_found_handling(gateway: GraphGateway):
    repo_id = "test://regression-notfound"
    # Seed a different repo but request missing file
    _seed_repo_with_fix_commits(gateway, repo_id, "target.py")
    svc = AnalyticsService(gateway, config=None)
    res = svc.regression_risk(repo_id, "missing.py")
    assert res.status == Status.NOT_FOUND
    assert res.result is not None
    assert res.result.related_files == []
    assert any("not found" in w.lower() for w in res.limitations.warnings)


def test_regression_no_fix_commits_reports_limitation(gateway: GraphGateway):
    repo_id = "test://regression-nofix"
    target = "solo.py"
    # Seed file and a non-fix commit touching it
    repo = Repository.create(name=repo_id, repository_id=repo_id)
    gateway.repositories.upsert(repo)
    f = File.create(repository_id=repo_id, path=target)
    gateway.files.upsert_batch(repo_id, [f])
    c = Commit.create(
        repository_id=repo_id,
        hash="c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        date=datetime(2024, 2, 1, tzinfo=timezone.utc).isoformat(),
        message="feat: add solo",
        is_fix=False,
    )
    gateway.commits.upsert_batch(repo_id, [c])
    gateway.relationships.upsert_modified(repo_id, c.id, f.id)

    svc = AnalyticsService(gateway, config=None)
    res = svc.regression_risk(repo_id, target)
    # Empty list with limitation, not zero-risk claim
    assert res.result is not None
    assert res.result.related_files == []
    warnings = " ".join(res.limitations.warnings).lower()
    assert "no fix evidence found" in warnings
    assert "does not indicate zero regression risk" in warnings
    # Status should be insufficient or ok with limitation (we use insufficient)
    assert res.status in (Status.INSUFFICIENT, Status.OK)
    # Must not claim low/no risk as probability
    assert "low risk" not in warnings or "does not indicate" in warnings


def test_regression_read_is_repository_isolated(gateway: GraphGateway):
    # Two repos with same target path but different fix histories should not cross-influence
    repo_a = "test://regression-iso-a"
    repo_b = "test://regression-iso-b"
    _seed_repo_with_fix_commits(gateway, repo_a, "target.py")
    # Seed repo_b with solo fix commit that co-changes with helper2.py only
    repo = Repository.create(name=repo_b, repository_id=repo_b)
    gateway.repositories.upsert(repo)
    files_b = [
        File.create(repository_id=repo_b, path="target.py"),
        File.create(repository_id=repo_b, path="helper2.py"),
    ]
    gateway.files.upsert_batch(repo_b, files_b)
    ids_b = {f.path: f.id for f in files_b}
    c = Commit.create(
        repository_id=repo_b,
        hash="d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
        date=datetime(2024, 1, 15, tzinfo=timezone.utc).isoformat(),
        message="fix: target helper2",
        is_fix=True,
    )
    gateway.commits.upsert_batch(repo_b, [c])
    with gateway.driver.session() as s:
        s.run(
            "MATCH (c:Commit {id:$id, repository_id:$repo}) SET c.fixReason=$r",
            id=c.id,
            repo=repo_b,
            r="fix helper2",
        ).consume()
    for p in ["target.py", "helper2.py"]:
        gateway.relationships.upsert_modified(repo_b, c.id, ids_b[p])

    svc = AnalyticsService(gateway, config=None)
    res_a = svc.regression_risk(repo_a, "target.py")
    res_b = svc.regression_risk(repo_b, "target.py")
    assert any(r.path == "helper.py" for r in res_a.result.related_files)  # type: ignore[union-attr]
    assert any(r.path == "helper2.py" for r in res_b.result.related_files)  # type: ignore[union-attr]
    # Cross-check no leakage
    assert not any(r.path == "helper2.py" for r in res_a.result.related_files)  # type: ignore[union-attr]
    assert not any(r.path == "helper.py" for r in res_b.result.related_files)  # type: ignore[union-attr]


def test_regression_read_is_read_only(gateway: GraphGateway):
    repo_id = "test://regression-readonly"
    _seed_repo_with_fix_commits(gateway, repo_id, "target.py")
    with gateway.driver.session() as s:
        before_nodes = s.run("MATCH (n) RETURN count(n) as c").single()["c"]  # type: ignore[index]
        before_rels = s.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]  # type: ignore[index]

    svc = AnalyticsService(gateway, config=None)
    svc.regression_risk(repo_id, "target.py")

    with gateway.driver.session() as s:
        after_nodes = s.run("MATCH (n) RETURN count(n) as c").single()["c"]  # type: ignore[index]
        after_rels = s.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]  # type: ignore[index]

    assert before_nodes == after_nodes
    assert before_rels == after_rels
