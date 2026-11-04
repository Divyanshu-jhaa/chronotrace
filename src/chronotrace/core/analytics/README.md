# Analytics — Contracts, Decay, Evidence

Deterministic, evidence-backed analytics over the repository graph.
No LLM, no random, no execution of repository code. Every number is
traceable: **component retrieval → decay → normalization → weighting → ranking**
with explicit insufficient-history handling.

## Result contract

Every analytics operation returns a typed envelope:

```python
from chronotrace.core.analytics.contracts import AnalyticsResult, Status, Limitations, Evidence

# status ∈ {ok, insufficient_history, not_found, error}
# repository_id: str
# target: str  (canonical POSIX path or scope)
# result: T | None  (per-capability payload)
# evidence: Evidence(commits, relationships)
# limitations: Limitations(shallow_history, commit_count, warnings)
```

Per-capability payloads:

* **Criticality** — `CriticalityResult(score, components{raw,normalized,weighted}, repo_stats, weights)`
* **Regression** — `RegressionResult(fix_commits, related_files{path,count,confidence})`
* **Impact** — `ImpactResult(importers, dependencies, co_changed{path,count,confidence,potential_hidden_coupling}, hidden_couplings)`
* **TestReco** — `TestRecoResult(candidates{path,score,reasons,evidence})`

All scores/rankings are deterministic for the same `(graph snapshot, half_life, now)`.
Ordering is deterministic (count → confidence → lexical path; score → lexical).

## Status vocabulary

* `ok` — sufficient history, authoritative result
* `insufficient_history` — evidence below documented threshold (shallow history, few observations) → result may still be returned but consumer must treat confidence as low
* `not_found` — repository or file not in graph
* `error` — validation or computation failure

Missing evidence is **never** presented as zero risk. Examples:

* no fix commits → limitation `"no fix evidence found — does not indicate zero regression risk"` (not a zero-rank)
* no co-change → `"insufficient co-change evidence — fewer than 3 observations"` or `insufficient_history`
* no import edge → `"no static import relationship resolved — absence of an edge does not prove absence of a dependency"`

Thresholds (centralized in `service.py`):

* `MIN_COMMITS_FOR_AUTHORITATIVE = 10`
* `MIN_TOUCHING = 3`

Shallow history (`commit_count < 10`) and diagnostics (`excluded_paths_count`, `parser_failures_count`) are propagated in `limitations.warnings`.

## Exponential time decay

Historical metrics (churn, fix involvement, co-change) are decayed by commit age:

```
w = 0.5 ** (age_days / half_life_days)
```

where `half_life_days = analysis.history.decay_half_life_days` (Pydantic-validated positive number, default 365). A commit one half-life old contributes half weight; two half-lives → quarter weight; age 0 → weight 1.0. Future-dated commits are clamped to age 0 (weight 1.0) with a limitation note. Fan-in (`IMPORTS` count) is structural and not decayed.

`now` is injectable (`service.criticality(..., now=FIXED_DATETIME)`) for determinism; tests and the fixed fixture pin `now`.

### Half-life formula example — `age == half_life → weight 0.5`

This example is the documented contract and is verified by unit tests and by
executing the assertion block below:

```python
from chronotrace.core.analytics.decay import decay_weight

assert decay_weight(0, 30) == 1.0          # age 0 -> full weight
assert decay_weight(30, 30) == 0.5         # age == half_life -> 0.5
assert decay_weight(60, 30) == 0.25        # age == 2 * half_life -> 0.25
assert abs(decay_weight(15, 30) - 0.7071067811865476) < 1e-9
```

Verification (doctest style):

```python
>>> from chronotrace.core.analytics.decay import decay_weight
>>> decay_weight(30, 30)
0.5

```

Run the assertion block:

```bash
python -c "from chronotrace.core.analytics.decay import decay_weight; assert decay_weight(30,30)==0.5; assert abs(decay_weight(60,30)-0.25)<1e-9; print('half-life ok')"
```

Unit tests also pin this: `tests/unit/test_analytics_fixture.py::test_fixture_commits_across_half_life_boundaries` asserts `decay_weight(HALF_LIFE, HALF_LIFE) == 0.5` within `1e-9`.

The fixed fixture (`tests/fixtures/analytics/`) uses `half_life_days=30` and `now=2024-04-01` with commits at ages `0, 7, 30, 60, 91` days, yielding weights `1.0, ~0.85, 0.5, 0.25, ~0.12` to exercise all four decay boundaries.

## Change criticality

Deterministic per-file score from four evidence components:

* **decayed churn** — `sum(decay_weight * (1 + log1p(additions+deletions)))` over commits touching file
* **fix involvement** — `sum(decay_weight for is_fix commits)`
* **co-change** — `sum(weight or count)` over `CO_CHANGED_WITH` / `CO_CHANGES` neighbors (already aggregated in graph)
* **fan-in** — `count distinct importers` via `IMPORTS` (not decayed)

Normalization is repository-scoped min-max per component:

```
norm = (raw - min) / (max - min)  if max > min else 0.5
```

Weights (heuristic, to be evaluated in Iteration 12 backtesting):

```python
WEIGHTS = {"churn": 0.30, "fix": 0.30, "cochange": 0.25, "fan_in": 0.15}
```

Final `score = sum(weight_i * norm_i)` in `[0, 1]` with per-component breakdown `{raw, normalized, weighted, score}` and evidence (commits, relationships). The fixed fixture pins `a.py` as highest (`score=1.0`, all components `normalized=1.0`) and `b.py`/`c.py` lower, verified within `1e-9`.

## Regression risk

For target file: find fix commits touching target, collect other files in those commits, rank by co-occurrence count and directional confidence `P(file|target) = fixes_with_both / fixes_with_target`, dedup commits/pairs, rank by count desc → confidence desc → lexical. Output is framed as **historical regression evidence, not a future-bug probability** (`"historical regression evidence — ranked by fix-commit co-occurrence and directional confidence P(file|target), not a future-bug probability"`).

Example from fixture (target `a.py`, fix commits `C2` and `C4`):

* `c.py` count 2, confidence 1.0
* `b.py` count 1, confidence 0.5
* `tests/test_a.py` count 1, confidence 0.5

No fix commits → empty ranking with limitation `"no fix evidence found — does not indicate zero regression risk"` (never a zero-risk claim).

## Historical impact

Retrieves `importers` (`<-IMPORTS-`), `dependencies` (`-IMPORTS->`), and `co_changed` (`-CO_CHANGED_WITH-`), compares co-change against import edges, and flags `potential_hidden_coupling=true` **only when no `IMPORTS` edge exists in either direction**, with note `"observed association not proven dependency"` (long form: `"observed co-change association, not proven dependency — may be indirect, test, or config coupling"`). Supporting commit evidence is included.

Fixture example (target `a.py`):

* importers: `b.py`, `tests/test_a.py`
* co-changed: `b.py` (not hidden, has import), `c.py` (hidden, no import), `tests/test_a.py` (not hidden)

Missing import edge is disclosed as `"no static import relationship resolved — absence of an edge does not prove absence of a dependency"`. Zero co-change → `insufficient_history`.

## Test recommendation

Enumerates candidates via `File.is_test` flag and naming patterns (`test_*.py`, `*_test.py`, `tests/**`, `*_tests.py`; excludes `conftest.py`), scores four signals with heuristic weights (`naming 1.0`, `imports 2.0`, `cochange 1.5`, `fix 1.0`), normalizes co-change/fix by max, and returns ranked candidates with `reasons` (`naming_convention`, `imports_target`, `co_change:N`, `fix_commit:N`). Ordered by score desc → lexical tie-break. No claim of real coverage (`limitations` disclaimer `"no claim of real coverage — recommendations are heuristic suggestions, not measured coverage or guaranteed relevance"`). Non-test files like `src/helper.py` are excluded even if they co-change heavily.

Fixture example (target `a.py`): `tests/test_a.py` ranks top with reasons `["naming_convention", "imports_target", "co_change:2", "fix_commit:1"]` and `score=5.5`.

## Fixed fixture

`tests/fixtures/analytics/` seeds repository `R-fixture` with:

* files `a.py`, `b.py`, `c.py`, `tests/test_a.py`
* commits `C1..C5` across half-life boundaries (see decay example above), `is_fix` on `C2`/`C4`
* `MODIFIED` edges with line counts, `CO_CHANGES` edges with `count/weight/confidence`, `IMPORTS` edges `b.py->a.py` and `tests/test_a.py->a.py`
* `expected.json` with expected scores/breakdowns/rankings (validated within `1e-9`)

Unit tests load the fixture into an in-memory fake; integration tests seed it into a disposable Neo4j via `testcontainers` (or `bolt://localhost:7687`) and run all four analytics via `AnalyticsService`.

```bash
pytest tests/unit/test_analytics_fixture.py -v
pytest tests/integration/test_analytics.py -v  # requires Docker
```

## Determinism and isolation

* `now` injection on every service method guarantees byte-identical replays.
* Every Cypher is parameterized and filtered by `repository_id`.
* No analytics code contains `MERGE`/`CREATE`/`DELETE`; read-only verified by code grep and by integration read-only node/relationship count checks.

## Limitations disclosure

History depth, excluded paths, and parser failures are propagated from static-analysis diagnostics into `limitations`. Consumers should surface `limitations.warnings` alongside scores, especially for `insufficient_history` / `shallow history` results.

## Verification

```bash
npx openspec validate --all --strict
pytest tests/unit/test_analytics_fixture.py tests/integration/test_analytics.py -v
python -m doctest src/chronotrace/core/analytics/README.md
python -c "from chronotrace.core.analytics.decay import decay_weight; assert decay_weight(30,30)==0.5; print('half-life ok')"
```
