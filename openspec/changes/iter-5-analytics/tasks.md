# Tasks: Iteration 5 — Deterministic Analytics and Evidence Contracts

## 1. Foundation — Contracts, Decay, and Graph Reads

- [ ] 1.1 Create `src/chronotrace/core/analytics/contracts.py` with `Status` enum and `AnalyticsResult[T]`, `Limitations`, `Evidence`, `CriticalityResult`, `RegressionResult`, `ImpactResult`, `TestRecoResult` dataclasses; verify `mypy` and `pytest tests/unit/test_analytics_contracts.py` covering status vocabulary and field presence.
- [ ] 1.2 Implement `src/chronotrace/core/analytics/decay.py` with `decay_weight`, `age_days`, future-date clamping, and `DECAY_HALF_LIFE_DAYS` config wiring; verify `pytest tests/unit/test_analytics_decay.py` covers zero-age=1.0, one half-life=0.5, two half-lives=0.25, future-date, invalid half-life, and consistent application.
- [ ] 1.3 Implement `src/chronotrace/core/analytics/repositories.py` (or `graph/analytics_repo.py`) with read-only parameterized Cypher helpers (`get_file`, `get_commits_touching_file`, `get_fix_commits_for_file`, `get_cochange_neighbors`, `get_importers`, `get_dependencies`, `get_test_candidates`, `get_repo_commit_count`); verify `pytest tests/unit/test_analytics_repositories.py` with fake driver asserts parameterized queries and `repository_id` scoping, and no `MERGE`/`CREATE` strings in `core/analytics/`.
- [ ] 1.4 Add `src/chronotrace/core/analytics/normalization.py` with repository-scoped `min_max_normalize` and `normalize_components`; verify `pytest tests/unit/test_analytics_normalization.py` covers normal range, identical values (max==min), single-file repo, and cross-repo isolation.

## 2. Change Criticality

- [ ] 2.1 Implement `src/chronotrace/core/analytics/criticality.py` retrieving decayed churn, fix involvement, co-change evidence, and fan-in; verify `pytest tests/unit/test_analytics_criticality.py` checks raw retrieval, decay application, and fan-in counting against a fixture.
- [ ] 2.2 Add normalization and weighted scoring (`WEIGHTS` heuristic constants documented) with per-component breakdown (`raw`, `normalized`, `weighted`, `score`); verify `pytest tests/unit/test_analytics_criticality.py` asserts normalization is repo-scoped, weights sum correctly, scores in [0,1], and breakdown is returned; verify determinism by running twice on same fake and comparing equality.
- [ ] 2.3 Wire criticality into `service.py` envelope (status/limitations/evidence) and handle empty/insufficient cases; verify `pytest tests/unit/test_analytics_criticality.py` covers `insufficient_history` for shallow history and `not_found` for missing file.

## 3. Regression Risk

- [ ] 3.1 Implement `src/chronotrace/core/analytics/regression.py` to find fix commits touching target, collect other files in those commits, compute co-occurrence counts and directional confidence `P(file|target)`, deduplicate commits/pairs; verify `pytest tests/unit/test_analytics_regression.py` with a 2-fix-commit fixture asserts ranking (helper count 2 > other count 1) and deduplication.
- [ ] 3.2 Integrate regression ranking into `service.py` with evidence (hashes, dates, messages, `fix_reason`) and output framing; verify `pytest tests/unit/test_analytics_regression.py` covers no-fix-commits case (empty list with "no fix evidence found" limitation, not zero-risk claim) and phrasing is evidence-not-probability.
- [ ] 3.3 Add integration check for regression reads; verify `pytest tests/integration/test_analytics_regression.py` against disposable Neo4j with seeded fix commits and `not_found` handling.

## 4. Historical Impact and Hidden Couplings

- [ ] 4.1 Implement `src/chronotrace/core/analytics/impact.py` retrieving importers, dependencies, and co-changed files via graph reads; verify `pytest tests/unit/test_analytics_impact.py` asserts correct sets for a fixture where `A->TARGET->B` and `TARGET` co-changes with `X` and `Y`.
- [ ] 4.2 Add co-change vs import comparison and `potential_hidden_coupling` flag (only when no `IMPORTS` edge in either direction, with "observed association not proven dependency" note); verify `pytest tests/unit/test_analytics_impact.py` checks flagged `X` vs unflagged `Y` (which has direct import) and that supporting commit evidence is included.
- [ ] 4.3 Wire impact into `service.py` with limitations disclosure (shallow history, excluded paths); verify `pytest tests/unit/test_analytics_impact.py` covers `insufficient_history` for zero co-change and "no static relationship resolved" disclaimer when no import edge exists.

## 5. Test Recommendation

- [ ] 5.1 Implement `src/chronotrace/core/analytics/test_reco.py` enumerating candidates via `is_test` flag and naming patterns (`test_*.py`, `*_test.py`, `tests/**`), scoring signals (naming, `IMPORTS` target, co-change, fix-commit involvement), and reason arrays; verify `pytest tests/unit/test_analytics_test_reco.py` asserts a candidate with all signals ranks above naming-only and that `src/helper.py` (non-test) is excluded.
- [ ] 5.2 Add ranking, tie-breaker (lexical), and integration into `service.py` with per-candidate `reasons` and disclaimer limitations ("no claim of real coverage"); verify `pytest tests/unit/test_analytics_test_reco.py` checks deterministic tie ordering and disclaimer presence.

## 6. Insufficient-History Handling and Service Facade

- [ ] 6.1 Implement centralized coverage logic in `service.py` (`MIN_COMMITS_FOR_AUTHORITATIVE`, `MIN_TOUCHING`) producing `not_found` vs `insufficient_history` vs `ok` with `Limitations` (shallow history, excluded paths, parser failures, warning strings); verify `pytest tests/unit/test_analytics_insufficient_history.py` covers empty fix history ≠ zero risk, missing co-change → `insufficient_history`, missing import edge disclaimer, shallow history disclosure, and excluded/parser counts propagation.
- [ ] 6.2 Expose `AnalyticsService` facade (`criticality`, `regression_risk`, `historical_impact`, `recommend_tests`, `get_file_history`) with injected `graph` and `config`, `now` parameter for determinism, and repository isolation on every query; verify `pytest tests/unit/test_analytics_service.py` shows cross-repo isolation (R1 ignores R2) and read-only behavior (no graph mutation counts).
- [ ] 6.3 Verify deterministic ordering and `Status` consistency across all capabilities; run `pytest tests/unit/test_analytics_determinism.py` that calls each operation twice on the same fixture and asserts byte-identical results.

## 7. Fixture, Integration, and Documentation

- [ ] 7.1 Create fixed fixture `tests/fixtures/analytics/` (seeded repository `R-fixture` with files `a.py`, `b.py`, `c.py`, `tests/test_a.py`, commits `C1..C5` across half-life boundaries, `is_fix` marks, `MODIFIED`, `CO_CHANGES`, `IMPORTS` edges) and `expected.json` with expected scores/breakdowns/rankings; verify `pytest tests/unit/test_analytics_fixture.py` loads fixture into fake and asserts scores and top-N rankings within `1e-9` tolerance and that `openspec validate --strict` passes.
- [ ] 7.2 Add integration test `tests/integration/test_analytics.py` that loads the fixture into disposable Neo4j (or `bolt://localhost:7687` test DB) and runs all four analytics via `AnalyticsService`; verify `pytest tests/integration/test_analytics.py -v` passes including empty/duplication cases and that every score has breakdown and every result has evidence.
- [ ] 7.3 Document analytics contracts and limitations in `docs/analytics.md` (or README section) and verify the documented half-life formula example (`age=half_life → weight 0.5`) matches the implementation by running the example as a doctest or unit assertion.
