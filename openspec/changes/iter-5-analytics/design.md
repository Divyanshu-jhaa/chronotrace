# Design: Iteration 5 — Deterministic Analytics and Evidence Contracts

## Context

After Iterations 1–4 ChronoTrace has: validated `chronotrace.yml` (Pydantic v2) providing `analysis.history.decay_half_life_days`, `graph.batch_size`, `repositories_dir`, exclusion patterns, and `is_test` heuristics; a shared Neo4j driver with repository-scoped stable IDs for `Repository`/`Commit`/`File`/`Author`, uniqueness constraints, `MERGE`-based idempotent writes, and parameterized sessions; repository acquisition via Git/PyDriller with `Commit.is_fix`/`fix_reason`/`REVERTS` and chronological writes; and a current file-level `IMPORTS` graph built from `ast` without execution plus diagnostics (`files_failed`, `unresolved_imports`). The graph contains `(:File)-[:CO_CHANGES {count, weight, confidence}]->(:File)` (historical, directional confidence `P(B|A)`) and `(:File)-[:IMPORTS]->(:File)` (current snapshot) alongside `(:Commit)-[:MODIFIED]->(:File)` and `(:File)<-[:CONTAINS]-(:Repository)`.

No analytics layer exists. MCP (Iter 6), agent (Iter 7), and API reports (Iter 8) all depend on deterministic, evidence-backed scores that the LLM must not invent. This design explains how to build that layer as pure Python over the graph, read-only, fully repository-isolated, and independently testable with fakes and a fixed fixture.

See `proposal.md` — Why/What Changes for motivation and `specs/analytics/spec.md` for normative behavior; this doc covers *how*.

Constraints:
- Deterministic, no LLM, no random, no execution of repo code.
- `decay_half_life_days` is validated Pydantic-positive; no new secrets.
- All Cypher is parameterized; no arbitrary Cypher exposure.
- Repository isolation: every read filtered by `repository_id`.
- Analytics is read-only; graph writes remain in Ingestion/Static Analysis.
- Python 3.12+, `pyproject.toml` stack unchanged (no new deps).

## Goals / Non-Goals

**Goals:**
- Provide a single `analytics` package with a shared result contract, decay helper, and four capability services invoked directly in Python and by future MCP tools.
- Make every number traceable: component retrieval → decay → normalization → weighting → ranking, each with evidence.
- Handle insufficient history explicitly (`insufficient_history`/`not_found`) without zero-risk claims.
- Keep the subsystem unit-testable without Neo4j via injected graph repositories / fakes; integration tests run against disposable Neo4j.
- Ship a fixed graph fixture producing stable expected scores for regression.

**Non-Goals:**
- SZZ, function-level tracking, real coverage, embeddings, multi-language, timeline UI (PLAN §7).
- Backtesting weight evaluation (Iteration 12) — weights ship as heuristic constants with docs.
- MCP server, agent loop, FastAPI routes, CLI, UI, Docker — later iterations.
- Writing or mutating graph state (analytics never calls `MERGE`/`CREATE`/`DELETE`).
- Caching/memoization of analytics results (can be added later without contract change).

## Decisions

### Decision 1 — Package decomposition

**Choice:** `src/chronotrace/core/analytics/` with focused modules plus a facade:

| Module | Responsibility |
|---|---|
| `contracts.py` | `Status` enum (`ok`, `insufficient_history`, `not_found`, `error`), dataclasses `AnalyticsResult[T]`, `Limitations`, `Evidence`, per-capability result types (`CriticalityResult`, `RegressionResult`, `ImpactResult`, `TestRecoResult`) |
| `decay.py` | `decay_weight(age_days, half_life) -> float = 0.5 ** (age/ half_life)`, `age_days(commit_date, now)`, future-date clamp, half-life validation |
| `normalization.py` | `min_max_normalize(values)`, `normalize_components(raw, repo_stats)` — repository-scoped min/max gathered from graph |
| `repositories.py` | Thin read-only graph access: `get_file`, `get_commits_touching_file`, `get_fix_commits_for_file`, `get_cochange_neighbors`, `get_importers`, `get_dependencies`, `get_commits_for_file_ids`, `get_test_candidates`, `get_repo_commit_count` — all parameterized Cypher |
| `criticality.py` | Retrieve 4 raw components, apply decay, gather repo min/max, normalize, weight-sum to `score`, build breakdown + evidence |
| `regression.py` | Fix commits for target → files in those commits → count + directional confidence `P(file|target) = fixes_with_both / fixes_with_target`, dedup, rank |
| `impact.py` | Importers/dependencies/co-change sets → compare → flag hidden couplings |
| `test_reco.py` | Enumerate candidate test files (naming heuristic + graph test flag), score per signal, rank, attach reasons |
| `service.py` | `AnalyticsService(graph, config)` facade exposing `criticality(repo_id, path)`, `regression_risk(...)`, `historical_impact(...)`, `recommend_tests(...)`, `file_history(...)` (5th helper for Iter 6); owns contract construction (status/limitations/evidence) and coordinates `now` injection for determinism |
| `models.py` | Shared evidence models reused by contracts |

**Rationale:** Matches the static-analysis decomposition (Iteration 4) that proved testable (parser/mapper/resolver/service split). Four capabilities have distinct query patterns and ranking logic; shared decay/contracts prevent duplication but keep scoring logic isolated.

**Alternatives:** Single `analytics.py` with 800+ lines — rejected: orthogonal test strategies (decay math vs fan-in counting vs co-change confidence) and independent future weight tuning would collide.

### Decision 2 — Common result contract as a generic dataclass

**Choice:**
```python
class Status(str, Enum):
    OK = "ok"
    INSUFFICIENT = "insufficient_history"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class Limitations:
    shallow_history: bool = False
    commit_count: int | None = None
    excluded_paths_count: int | None = None
    parser_failures_count: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class AnalyticsResult(Generic[T]):
    status: Status
    repository_id: str
    target: str  # canonical POSIX path or scope string
    result: T | None  # capability-specific payload (scores, rankings)
    evidence: Evidence  # commits, relationships, components used
    limitations: Limitations
```

Per-capability payloads:
- `CriticalityResult { score, components {raw, normalized, weighted}, repo_stats }`
- `RegressionResult { fix_commits: list[FixCommit], related_files: list[RankedFile {path, count, confidence}] }`
- `ImpactResult { importers, dependencies, co_changed, hidden_couplings: list[Flagged] }`
- `TestRecoResult { candidates: list[RankedTest {path, score, reasons, evidence}] }`

`Evidence` carries `commits: list[CommitEvidence {hash, date, message, fix_reason}]`, `relationships: list[RelEvidence]`, `diagnostics_ref` when relevant.

**Rationale:** One contract satisfies PLAN's "status, repository identifier, target file or query scope, result values and rankings, evidence, limitations" and is already required by Iter 6 MCP tools. Generic `AnalyticsResult[T]` keeps typing without duplicating envelope logic.

**Alternatives:** Per-capability bespoke envelope — rejected (MCP/API would need per-tool ad-hoc mapping); plain dict — rejected (no typing, no validation).

### Decision 3 — Exponential decay implementation

**Choice:**
```python
def decay_weight(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0: raise ValueError
    age = max(0.0, age_days)
    return 0.5 ** (age / half_life_days)

def age_days(commit_date: datetime, now: datetime) -> float:
    # both timezone-aware or both naive; normalize to UTC, diff in seconds / 86400
```

- `now` is injected (default `datetime.now(timezone.utc)`) so tests/fixture use a fixed reference for determinism.
- Applied at query time: weight multiplied onto per-commit contributions when aggregating churn/fix/co-change counts. Fan-in (current snapshot `IMPORTS`) is not decayed (structural, not historical).
- Bulk formula uses `math.pow` / `**` with float; tolerance `1e-12` in tests.

**Rationale:** Directly implements PLAN's `decay_half_life_days`; half-life semantics (50% at `half_life`) is the natural exponential with base 0.5. Injection of `now` is required for deterministic fixture regression.

**Alternatives:** `exp(-ln2 * age / half_life)` — mathematically identical; chosen `0.5 ** ratio` is clearer and directly names the half-life. Storing pre-decayed weights in Neo4j — rejected (half-life is configurable, would require bulk recompute).

### Decision 4 — Criticality components, normalization, weighting

**Choice:**

Raw retrieval (all `WHERE repository_id=$repo`, `MATCH` via `File.canonical_path` + `Repository`):

- **Churn**: `MATCH (c:Commit)-[m:MODIFIED]->(f:File {repo, path})` return `c.date, m.additions, m.deletions`; decayed churn = `sum(decay_weight(c) * (1 + log1p(add+del) or lines or 1))` — two variants considered; default `sum(weight)` (commit count weighted) plus optional bytes term. Chose weighted commit count + normalized lines to avoid zero-vs-null conflation (unknown lines stored as null per Iter 3). Tests pin the exact formula.
- **Fix involvement**: same commits where `c.is_fix=true`, `sum(decay_weight(c))`.
- **Co-change**: `MATCH (f)-[r:CO_CHANGES]->(o) or <-` return `r.count, r.weight, r.confidence`; for target `T`, raw = `sum(r.weight)` or `degree`, decay applied if edge carries per-commit provenance; if edge already aggregated without dates, use stored weight/confidence directly.
- **Fan-in**: `MATCH (other)-[:IMPORTS]->(f)` count distinct importers (no decay).

Normalization: repository-scoped min-max per component (gather `min/max` across all `File` nodes in repo for that component, or compute distribution from sampled query). Formula `norm = (raw - min) / (max - min)` when `max>min`, else `0.5` or `0.0` with documentation; avoids divide-by-zero for single-file repos. Future work may swap to z-score without contract change.

Weighting: code constants `WEIGHTS = {churn:0.30, fix:0.30, cochange:0.25, fan_in:0.15}` marked `# heuristic — evaluate in Iteration 12 backtesting`. Final `score = sum(weight_i * norm_i)`. Score range `[0,1]`.

Evidence: per component, the contributing commits/relationships (hashes, dates trimmed) and repo stats (`min/max/count`).

**Rationale:** Components match PLAN §2.1 + Iteration 5 spec verbatim; min-max is simplest explainable normalization that preserves ranking. Heuristic weights are the only safe default before backtest data exists.

**Alternatives:** No normalization (raw weighted sum) — rejected (fan-in vs churn magnitudes incomparable across repos). Z-score — deferred (requires mean/std, sensitive to outliers in small repos).

### Decision 5 — Regression risk ranking (count + directional confidence)

**Choice:**
```cypher
// Fix commits touching target
MATCH (c:Commit {repository_id:$repo, is_fix:true})-[:MODIFIED]->(t:File {repository_id:$repo, canonical_path:$target})
RETURN c.hash, c.date, c.message, c.fix_reason

// Other files in those fix commits
MATCH (c:Commit {hash in $fixHashes})-[:MODIFIED]->(other:File)
WHERE other.canonical_path <> $target
RETURN other.canonical_path, collect(c.hash) as commits, count(*) as cnt
ORDER BY cnt DESC
```

- Directional confidence for related file `F`: `conf = |fixCommits(T) ∩ fixCommits(F)| / |fixCommits(T)|` — proportion of target's fix commits that also touched `F`. Stored co-change confidence `P(F|T)` can corroborate when available.
- Deduplication: hashes are deduped via `DISTINCT`; pair counts use set semantics.
- Rank by `cnt` desc, then `confidence` desc, then lexical path for determinism.

**Rationale:** Directly maps PLAN "retrieve historical fix commits that modified a target file; identify files modified alongside; rank using co-occurrence count and directional confidence".

### Decision 6 — Historical impact and hidden-coupling detection

**Choice:**
```cypher
-- importers:  MATCH (i:File)-[:IMPORTS]->(t {repo, path})
-- dependencies: MATCH (t)-[:IMPORTS]->(d:File)
-- co-changed: MATCH (t)-[r:CO_CHANGES]-(o:File) RETURN o, r.count, r.weight, r.confidence
```

Compare sets: `co_changed_paths - (importers ∪ dependencies ∪ {target})` → candidates. For each candidate, check for any `IMPORTS` edge in either direction (`MATCH (a)-[:IMPORTS]-(b)`); if none, flag `potential_hidden_coupling=true` with note "observed co-change association, not proven dependency — may be indirect, test, or config coupling". Supporting evidence is the intersection of commit hashes that produced the co-change (via `MODIFIED` overlap or stored `CO_CHANGES` provenance).

**Rationale:** Implements PLAN's "combine current static import relationships with historical co-change; flag co-change without direct import as potential hidden couplings, not proven dependencies."

### Decision 7 — Test recommendation heuristics

**Choice:**

- Candidate enumeration: `MATCH (f:File {repo}) WHERE f.is_test = true OR f.canonical_path matches test patterns` → Patterns: `test_*.py`, `*_test.py`, `tests/**`, `test/**`, `*_tests.py`, `conftest.py` excluded (`is_test` from Iter 2/3 drives primary filter, patterns corroborate).
- Scoring per candidate `C` for target `T`:
  - `w_naming` if `matches_pattern(C, T)` (e.g., `test_foo` vs `foo`);
  - `w_imports` if `C -[:IMPORTS]-> T` (test imports target);
  - `w_cochange` = normalized `co_change_count(C,T)` / max;
  - `w_fix` = `count(fetch_fix_commits_involving_both(C,T))`.
  - Combined `score = w_n*naming + w_i*imports + w_c*cochange_norm + w_f*fix_norm`, weights heuristic-documented.
- Reasons array per candidate records which signals fired (`naming_convention`, `imports_target`, `co_change:N`, `fix_commit:N`).
- Ordered `score` desc, lexical tie-break; disclaimer in `limitations.warnings` that ranking is heuristic, not coverage.

### Decision 8 — Insufficient-history policy and thresholds

**Choice:** Centralized `coverage.py` or logic in `service.py`:

- `NOT_FOUND`: `File` node missing for `target` (checked via `MATCH (f {repo, path})`).
- `INSUFFICIENT_HISTORY` when: total repo commits `< MIN_COMMITS_FOR_AUTHORITATIVE` (e.g., 10) or target-touching commits `< MIN_TOUCHING` (e.g., 3) or co-change degree `==0` for impact/criticality's co-change component or fix commits `==0` for regression (returns `insufficient_history` or `ok` with limitation — see spec). Thresholds are constants documented in `contracts.py` and tested; spec defines phrasing verbatim ("no fix evidence found — does not mean zero risk", "insufficient co-change evidence", etc.).
- For shallow history that still allows a best-effort score, return `ok` but add `limitations.warnings` with commit count and confidence disclaimer; for below-threshold where an authoritative score would mislead, return `insufficient_history`.
- Always propagate `excluded_paths_count` / `parser_failures_count` from static-analysis diagnostics (queried via `Repository` or `AnalysisDiagnostics` node if persisted; otherwise passed through service) into `limitations`.

### Decision 9 — Graph queries, isolation, and testability

**Choice:**
- All Cypher is parameterized (`$repository_id`, `$target_path`, `$hashes`, etc.); `repository_id` predicate on every `MATCH`.
- Read-only: analytics never uses `MERGE`/`CREATE`/`DELETE`/`SET`.
- Repository patternConstructor injection `graph: AnalyticsGraphGateway` interface with methods above; production implementation uses `neo4j.Driver` session; unit tests inject an in-memory fake implementing the same interface; integration tests use disposable Neo4j (testcontainers or local `bolt://localhost:7687` with random database).
- Batch concern: analytics reads are small (per-file), no batching needed; fan-in/co-change queries use `UNWIND` only if multiple targets requested (not in MVP).
- `now` injection on service methods for determinism: `service.criticality(repo_id, path, now=FIXED_DATETIME)`.

### Decision 10 — Fixed fixture for regression

**Choice:** `tests/fixtures/analytics/` with `fixture.cypher` or Python builder that `MERGE`s a small repo `R-fixture` with files `a.py`, `b.py`, `c.py`, `tests/test_a.py`, commits `C1..C5` spanning dates covering half-life boundaries, `is_fix` marks, `MODIFIED` edges, `CO_CHANGES` edges with known counts/weights, and `IMPORTS` edges `b->a`, `test_a->a`. Checked-in `expected.json` stores expected `criticality` scores (to 1e-9), `regression` ranking, `impact` flagging, and `test_reco` ordering. Unit tests load fixture into fake; integration test optionally loads into Neo4j.

## Risks / Trade-offs

- **[Risk] Zero vs null line stats conflation** → Mitigation: preserve `null` for unknown additions/deletions (Iter 3 contract); churn formula treats `null` as `1` (presence) not `0`, and documents the fallback.
- **[Risk] Min-max normalization unstable on tiny repos (single file, all same value)** → Mitigation: when `max==min`, normalized value `0.5` or `0.0` per documented rule; limitation notes "insufficient distribution — normalized values are low confidence".
- **[Risk] Half-life misconfiguration (days vs hours) silently skews scores** → Mitigation: Pydantic validation + startup log of effective half-life; tests include boundary cases (1 day vs 365 days) pinned to fixture.
- **[Risk] Co-change confidence direction confusion (T->F vs F->T)** → Mitigation: compute directionally as `P(F|T)` for regression and use stored `CO_CHANGES.confidence` consistently; tests pin direction.
- **[Risk] Hidden coupling over-flagging (co-change due to test fixture churn, not architectural coupling)** → Mitigation: language in spec/design explicitly calls it "potential" observed association; score not elevated as proven dependency; include raw co-change count so consumer can threshold.
- **[Risk] Non-test file recommended as test** → Mitigation: primary filter `File.is_test` from Iter 2; pattern matching is corroborating, not sole signal; tests assert non-test helpers are excluded.
- **[Risk] Cross-repo leakage if `repository_id` filter omitted** → Mitigation: every query's first predicate is `repository_id=$repo`; code review gate + integration test with two repos sharing paths but diverging histories.
- **[Risk] Analytics writes accidentally mutate graph** → Mitigation: code audit—no `MERGE`/`CREATE` strings in `core/analytics/`; unit fake asserts call counts only for reads; CI grep for banned keywords in analytics package.
- **[Risk] Determinism broken by wall-clock `now`** → Mitigation: injectable `now` parameter defaulting to `utcnow`, fixed in tests/fixture.
- **[Risk] Performance on large repos (10k commits, fan-in 500)** → Mitigation: indexed fields (`Commit.repository_id + hash`, `File.repository_id + canonical_path`, `Commit.is_fix` composite where helpful); queries use `LIMIT` and per-file scope—no full graph scan. Sufficient for MVP; future iteration can add materialized views if needed.

## Migration Plan

No schema migration. Analytics is additive and read-only; shipping is code-only:

1. Ship `core/analytics/` and read-only graph repositories; no Cypher migrations, no new Neo4j constraints/indexes beyond Iter 2.
2. Existing ingested repositories require no backfill; analytics computes on demand from current graph state.
3. Deployment: part of existing `agent-api` / backend image; no new Compose service. Run design validation (`openspec validate --strict` passes) and fixture regression before merging.
4. Rollback: revert package import; graph unchanged (no data to migrate back). Feature-flag via caller — MCP/API can gate tool exposure without graph change.

## Open Questions

- **Should criticality weights become configurable via `chronotrace.yml` in this iteration or stay as code constants until Iteration 12 backtesting?** Decision: stay as code constants marked heuristic; exposing config now would imply tuning that the backtest must justify. No spec or task impact — a future config key `analysis.criticality_weights` can be added compatibly.
- **Should `file_history` (5th MCP tool's backing query) live in analytics or ingestion?** Treated as an analytics read helper (commit history for a file with limit) in `service.py`; ingestion owns writes. No spec impact if relocated.
- **Exact numeric threshold for `insufficient_history` (commit counts, co-change minima)** — documented as constants with rationale and tested; tunable without contract change, so deferred to implementation review.

