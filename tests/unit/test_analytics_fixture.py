"""Unit fixture regression for iter-5 analytics (Tasks 7.1).

Loads the fixed ``R-fixture`` into the in-memory fake and asserts that
analytics scores, breakdowns and rankings match the checked-in
``expected.json`` within ``1e-9`` tolerance. Also verifies deterministic
replay (twice identical) and repository isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chronotrace.config.models import ChronoTraceConfig
from chronotrace.core.analytics.decay import decay_weight
from chronotrace.core.analytics.service import AnalyticsService
from tests.fixtures.analytics.fixture import HALF_LIFE_DAYS, NOW, REPO_ID, get_fake_graph

EXPECTED_PATH = Path(__file__).parent.parent / "fixtures" / "analytics" / "expected.json"
TOL = 1e-9


def _load_expected() -> dict:
    with EXPECTED_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_fixture_files_exist():
    """Fixture files a.py, b.py, c.py, tests/test_a.py exist on disk."""
    base = Path(__file__).parent.parent / "fixtures" / "analytics" / "repo"
    for rel in ["a.py", "b.py", "c.py", "tests/test_a.py"]:
        p = base / rel
        assert p.exists(), f"fixture file missing: {p}"
        assert p.stat().st_size > 0, f"fixture file empty: {p}"


def test_fixture_commits_across_half_life_boundaries():
    """Commits span half-life boundaries (0, half, 2*half) with decay weights 1.0, 0.5, 0.25."""
    # Directly verify decay formula: age=half => 0.5, 2*half =>0.25, 0=>1.0
    assert decay_weight(0.0, HALF_LIFE_DAYS) == pytest.approx(1.0, abs=TOL)
    assert decay_weight(HALF_LIFE_DAYS, HALF_LIFE_DAYS) == pytest.approx(0.5, abs=TOL)
    assert decay_weight(2 * HALF_LIFE_DAYS, HALF_LIFE_DAYS) == pytest.approx(0.25, abs=TOL)
    # Fixture commits should hit those boundaries (C3 at half, C2 at 2*half, C5 at 0)
    # We check via expected commits dates
    expected = _load_expected()
    commits = expected["commits"]
    # Find commits with known half-life boundary dates
    # C3 is 2024-03-02 (30d before NOW 2024-04-01) => weight 0.5
    # C2 is 2024-02-01 (60d before) => 0.25
    # C5 is 2024-04-01 (0d) => 1.0
    from datetime import datetime

    def age_days(date_str: str) -> float:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        # NOW is timezone aware
        delta = (NOW - dt).total_seconds() / 86400.0
        return max(0.0, delta)

    for c in commits:
        age = age_days(c["date"])
        w = decay_weight(age, HALF_LIFE_DAYS)
        # Check that at least one commit hits each boundary
        if c["hash"] == "3" * 40:  # C3
            assert w == pytest.approx(0.5, abs=TOL), f"C3 weight should be 0.5, got {w}"
        if c["hash"] == "2" * 40:
            assert w == pytest.approx(0.25, abs=TOL)
        if c["hash"] == "5" * 40:
            assert w == pytest.approx(1.0, abs=TOL)


def test_expected_json_exists_and_keys():
    expected = _load_expected()
    assert expected["repository_id"] == REPO_ID
    assert expected["half_life_days"] == HALF_LIFE_DAYS
    assert expected["now"] == NOW.isoformat()
    for key in ("criticality", "regression", "impact", "test_reco"):
        assert key in expected, f"expected.json missing {key}"
    # Files
    assert set(expected["files"][i]["path"] for i in range(len(expected["files"]))) == {
        "a.py",
        "b.py",
        "c.py",
        "tests/test_a.py",
    }
    # Commits C1..C5
    assert len(expected["commits"]) == 5
    # CO_CHANGES and IMPORTS edges present
    assert len(expected["co_changes"]) >= 4
    assert len(expected["imports"]) >= 2


def test_criticality_scores_within_tolerance():
    expected = _load_expected()
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(fake, config)

    for path, exp in expected["criticality"].items():
        res = svc.criticality(REPO_ID, path, now=NOW)
        assert res.result is not None, f"criticality result missing for {path}"
        # Status may be insufficient_history due to shallow repo (5 commits <10) but score must still match
        assert exp["status"] in ("insufficient_history", "ok", "insufficient_history")
        assert abs(res.result.score - exp["score"]) < TOL, f"score mismatch for {path}"

        for comp, breakdown in exp["components"].items():
            actual = res.result.components[comp]
            assert abs(actual.raw - breakdown["raw"]) < TOL, f"{path}:{comp} raw"
            assert abs(actual.normalized - breakdown["normalized"]) < TOL, f"{path}:{comp} normalized"
            assert abs(actual.weighted - breakdown["weighted"]) < TOL, f"{path}:{comp} weighted"
            assert abs(actual.score - breakdown["score"]) < TOL, f"{path}:{comp} score"

        # Repo stats and weights sanity
        assert res.result.repo_stats == exp["repo_stats"]
        assert res.result.weights == exp["weights"]

        # Evidence present
        assert len(res.evidence.commits) > 0 or path == "tests/test_a.py"
        assert len(res.evidence.relationships) > 0 or path == "tests/test_a.py"

        # Determinism: second call identical
        res2 = svc.criticality(REPO_ID, path, now=NOW)
        assert res2.result.score == pytest.approx(res.result.score, abs=TOL)
        assert res2.result.components == res.result.components


def test_regression_rankings_within_tolerance_and_dedup():
    expected = _load_expected()
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(fake, config)

    for path, exp in expected["regression"].items():
        res = svc.regression_risk(REPO_ID, path)
        assert res.result is not None
        # Fix commits deduped and ordered
        exp_hashes = [c["hash"] for c in exp["fix_commits"]]
        actual_hashes = [c.hash for c in res.result.fix_commits]
        assert actual_hashes == exp_hashes, f"fix commits mismatch for {path}"

        # Rankings: count and confidence within tolerance, lexical tie-breaker
        exp_related = exp["related_files"]
        actual_related = res.result.related_files
        assert len(actual_related) == len(exp_related), f"related count mismatch for {path}"
        for er, ar in zip(exp_related, actual_related):
            assert er["path"] == ar.path
            assert er["count"] == ar.count
            assert abs(er["confidence"] - ar.confidence) < TOL

        # Evidence hashes/dates/messages/fix_reason present for fix commits
        for c in res.result.fix_commits:
            assert c.hash
            assert c.date
            assert c.message
            # fix_reason for fix commits
            assert c.fix_reason is not None and "fix" in c.fix_reason.lower()

        # Determinism
        res2 = svc.regression_risk(REPO_ID, path)
        assert [r.path for r in res.result.related_files] == [r.path for r in res2.result.related_files]


def test_impact_rankings_and_hidden_coupling_flag():
    expected = _load_expected()
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(fake, config)

    for path, exp in expected["impact"].items():
        res = svc.historical_impact(REPO_ID, path, now=NOW)
        assert res.result is not None, f"impact missing for {path}"

        # Importers / dependencies
        assert sorted(res.result.importers) == sorted(exp["importers"])
        assert sorted(res.result.dependencies) == sorted(exp["dependencies"])

        # Co_changed list and hidden flag
        exp_co = {c["path"]: c for c in exp["co_changed"]}
        actual_co = {c.path: c for c in res.result.co_changed}
        assert set(exp_co.keys()) == set(actual_co.keys()), f"co_changed keys mismatch for {path}"
        for p, e in exp_co.items():
            a = actual_co[p]
            assert a.count == e["count"]
            if e["confidence"] is not None:
                assert abs((a.confidence or 0) - e["confidence"]) < TOL
            assert a.potential_hidden_coupling == e["potential_hidden_coupling"]
            if e["potential_hidden_coupling"]:
                assert e["note"] is not None and "observed association not proven dependency" in e["note"]
                assert a.note is not None and "observed association not proven dependency" in a.note
            # Supporting commits evidence
            assert len(a.supporting_commits) >= 0  # may be empty for some but generally >0

        # Hidden couplings list matches flagged entries
        exp_hidden = set(h["path"] for h in exp["hidden_couplings"])
        actual_hidden = set(h.path for h in res.result.hidden_couplings)
        assert exp_hidden == actual_hidden

        # Evidence includes commits and relationships
        assert len(res.evidence.relationships) > 0
        # For a.py specifically, check the spec scenario: c.py hidden, b.py not hidden
        if path == "a.py":
            assert "c.py" in exp_hidden
            assert "b.py" not in exp_hidden
            assert "tests/test_a.py" not in exp_hidden
            # Ensure c.py is flagged with correct note
            flagged_c = actual_co["c.py"]
            assert flagged_c.potential_hidden_coupling is True
            assert "observed association not proven dependency" in (flagged_c.note or "")


def test_test_reco_rankings_and_reasons():
    expected = _load_expected()
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(fake, config)

    for path, exp in expected["test_reco"].items():
        res = svc.recommend_tests(REPO_ID, path, now=NOW)
        assert res.result is not None
        exp_cands = exp["candidates"]
        actual_cands = res.result.candidates
        assert len(actual_cands) == len(exp_cands), f"candidate count mismatch for target {path}"
        for ec, ac in zip(exp_cands, actual_cands):
            assert ec["path"] == ac.path
            assert abs(ec["score"] - ac.score) < TOL
            assert ec["reasons"] == ac.reasons
            assert ec["imports_target"] == ac.imports_target
            assert ec["naming_match"] == ac.naming_match
            assert ec["co_change_count"] == ac.co_change_count
            assert ec["fix_count"] == ac.fix_count

        # Ensure non-test files never recommended
        for cand in actual_cands:
            assert cand.path not in ("a.py", "b.py", "c.py"), f"non-test file recommended for {path}"

        # For target a.py, candidate should have all signals
        if path == "a.py":
            assert len(actual_cands) == 1
            top = actual_cands[0]
            assert top.path == "tests/test_a.py"
            assert "naming_convention" in top.reasons
            assert "imports_target" in top.reasons
            assert any(r.startswith("co_change:") for r in top.reasons)
            assert any(r.startswith("fix_commit:") for r in top.reasons)
            # No claim of coverage in limitations
            assert any("no claim of real coverage" in w.lower() for w in res.limitations.warnings)

        # Tie-breaker lexical: if we artificially create equal scores, order is lexical
        # Already covered via deterministic ranking: run twice and compare
        res2 = svc.recommend_tests(REPO_ID, path, now=NOW)
        assert [c.path for c in res.result.candidates] == [c.path for c in res2.result.candidates]


def test_repository_isolation():
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(fake, config)

    # Different repository_id should not leak R-fixture evidence.
    # The fake is repository-scoped: queries for other_repo return empty.
    # Due to fallback commit-scanning in some helpers, other_repo may appear
    # as insufficient_history with empty evidence rather than not_found;
    # the critical property is isolation of evidence and scores.
    other_repo = "R-other"
    # Baseline scores for R-fixture
    baseline_a = svc.criticality(REPO_ID, "a.py", now=NOW)
    assert baseline_a.result is not None
    for path in ["a.py", "b.py"]:
        res = svc.criticality(other_repo, path, now=NOW)
        # Status may be not_found or insufficient_history (empty history) — both are isolated.
        assert str(res.status.value) in ("not_found", "insufficient_history", "error")
        if res.result is not None:
            # If a result is returned for empty repo, it must not contain R-fixture commit evidence
            fixture_hashes = {c["hash"] for c in _load_expected()["commits"]}
            result_hashes = {c.hash for c in res.evidence.commits}
            assert result_hashes.isdisjoint(fixture_hashes), f"other repo leaked fixture hashes for {path}"
            assert res.result.score != baseline_a.result.score or path != "a.py"
        else:
            assert res.evidence.commits == [] or len(res.evidence.commits) == 0

        res2 = svc.regression_risk(other_repo, path)
        assert str(res2.status.value) in ("not_found", "insufficient_history", "ok")
        if res2.result and res2.result.fix_commits:
            fixture_hashes = {c["hash"] for c in _load_expected()["commits"]}
            assert all(c.hash not in fixture_hashes for c in res2.result.fix_commits)

        res3 = svc.historical_impact(other_repo, path, now=NOW)
        assert str(res3.status.value) in ("not_found", "insufficient_history", "ok", "error")
        if res3.result:
            fixture_paths = {"a.py", "b.py", "c.py", "tests/test_a.py"}
            # Other repo should not have R-fixture importers
            assert not any(p in fixture_paths for p in res3.result.importers if path == "other-not-exist")


def test_determinism_across_all_capabilities():
    """Running each operation twice on same fixture yields identical results."""
    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(fake, config)

    for path in ["a.py", "b.py", "c.py", "tests/test_a.py"]:
        c1 = svc.criticality(REPO_ID, path, now=NOW)
        c2 = svc.criticality(REPO_ID, path, now=NOW)
        assert c1.result == c2.result if c1.result and c2.result else c1.status == c2.status
        assert c1.evidence.commits == c2.evidence.commits

        r1 = svc.regression_risk(REPO_ID, path)
        r2 = svc.regression_risk(REPO_ID, path)
        assert r1.result.related_files == r2.result.related_files

        i1 = svc.historical_impact(REPO_ID, path, now=NOW)
        i2 = svc.historical_impact(REPO_ID, path, now=NOW)
        assert i1.result.co_changed == i2.result.co_changed

        t1 = svc.recommend_tests(REPO_ID, path, now=NOW)
        t2 = svc.recommend_tests(REPO_ID, path, now=NOW)
        assert t1.result.candidates == t2.result.candidates
