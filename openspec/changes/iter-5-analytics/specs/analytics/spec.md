# Spec Delta

## Purpose

Provides deterministic, evidence-backed analytics over the historical and structural graph — change criticality scoring, regression risk, historical impact, and test recommendation — with a shared result contract, exponential time decay, and explicit insufficient-history handling so no LLM invents scores.

## ADDED Requirements

### Requirement: Common analytics result contract

The system SHALL return every analytics operation result in a shared typed contract containing `status`, `repository_id`, `target` (file path or scope), `result` (values/rankings), `evidence` (commits, relationships, components used), and `limitations`/`data_coverage` (history depth, excluded paths, parser failures, warning strings), keeping numeric results separate from evidence-sufficiency assessment.

#### Scenario: Successful result contains all contract fields

- **WHEN** `get_change_criticality(repository_id="repo-1", file="src/foo.py")` is called for a known file with sufficient history
- **THEN** the result has `status="ok"`, `repository_id="repo-1"`, `target="src/foo.py"`, `result` with score/rankings, `evidence` listing source commits and relationship counts, and `limitations` describing coverage

#### Scenario: Not-found file returns structured not_found status

- **WHEN** an analytics operation is called for a `repository_id` or file path that does not exist in the graph
- **THEN** the result has `status="not_found"`, preserves `repository_id` and `target`, has empty `result`, and `limitations` explains that the file was not found

#### Scenario: Numeric results are not conflated with sufficiency

- **WHEN** a historical metric has no evidence (e.g., no fix commits for regression risk)
- **THEN** the system does not return a numeric zero-risk score; it returns `status="insufficient_history"` or `status="ok"` with empty evidence and a limitation stating "no fix evidence found — does not mean zero risk"

### Requirement: Analytics status vocabulary

The system SHALL use the status values `ok`, `insufficient_history`, `not_found`, and at most one additional failure status (e.g., `error`) and SHALL map them consistently across all four capabilities; `insufficient_history` SHALL be used when evidence is below the documented threshold for an authoritative score/ranking rather than fabricating a low score.

#### Scenario: Insufficient history status for sparse co-change

- **WHEN** historical impact is requested for a file with fewer than the documented minimum co-change observations
- **THEN** status is `insufficient_history` with `limitations` stating "insufficient co-change evidence — fewer than N observations"

#### Scenario: OK status for sufficient evidence even when results are empty

- **WHEN** regression risk is requested and there are fix commits but no co-changed files
- **THEN** status is `ok` with an empty ranked list and evidence showing the fix commits, plus a note that no co-changed files were observed

### Requirement: Exponential time decay

The system SHALL apply configurable exponential time decay to every historical metric that uses recency, using `decay_half_life_days` from validated configuration (Pydantic-validated positive number, default 365), where weight `w = 0.5 ^ (age_days / half_life)` so a commit one half-life old contributes half the weight of a current commit, and SHALL apply decay consistently across churn, fix involvement, and co-change.

#### Scenario: Decay at zero age is full weight

- **WHEN** a commit's date equals the reference "now" (age 0 days) with `half_life=30`
- **THEN** its decay weight is `1.0`

#### Scenario: Decay at one half-life is half weight

- **WHEN** a commit is exactly `half_life` days old
- **THEN** its weight is `0.5` within floating-point tolerance `1e-9`

#### Scenario: Decay at two half-lives is quarter weight

- **WHEN** a commit is `2 * half_life` days old
- **THEN** its weight is `0.25` within tolerance `1e-9`

#### Scenario: Future commit dates are clamped

- **WHEN** a commit date is in the future relative to reference now
- **THEN** age is treated as `0` days (weight `1.0`) and a limitation note is recorded that the commit has a future date

#### Scenario: Invalid half-life is rejected at config load

- **WHEN** `decay_half_life_days` is `0`, negative, or non-numeric
- **THEN** configuration validation fails with an actionable error naming the field and the positive-number requirement, and analytics is not invoked

#### Scenario: Decay applied consistently across metrics

- **WHEN** change criticality computes decayed churn and decayed fix involvement for the same file and commit set
- **THEN** both components use the identical half-life and reference date

### Requirement: Change criticality scoring

The system SHALL compute a deterministic per-file criticality score from four components: decayed churn, decayed fix involvement, weighted co-change evidence, and import fan-in. Raw evidence SHALL be retrieved from the graph, normalized within-repository via documented min-max, combined with explicit heuristic weights, and returned with final score, per-component breakdown, and source evidence.

#### Scenario: Criticality returns score with breakdown

- **WHEN** change criticality is computed for `a.py` in a repository with mixed history (several commits, some fixes, several co-change neighbors, several importers)
- **THEN** the result contains `score` (0–1 or documented range), `components { churn, fix_involvement, cochange, fan_in }` each with raw, normalized, and weighted values, and `evidence` listing the contributing commits and relationship counts

#### Scenario: Fan-in included correctly

- **WHEN** `a.py` is imported by `b.py` and `c.py` via `IMPORTS` edges
- **THEN** its fan-in component counts `2` before normalization

#### Scenario: Normalization is repository-scoped

- **WHEN** two repositories have files with identical raw churn but different repository maxima
- **THEN** their normalized churn values differ because normalization uses the repository's own min/max, not a global constant

#### Scenario: Weights are documented and heuristic-marked

- **WHEN** a consumer reads the criticality result or docs
- **THEN** the weights used are exposed (e.g., `{ churn: 0.3, fix: 0.3, cochange: 0.25, fan_in: 0.15 }`) and marked as heuristic until backtesting

#### Scenario: Deterministic for same snapshot and config

- **WHEN** change criticality is computed twice on the same graph snapshot and config
- **THEN** the score and breakdown are byte-identical

### Requirement: Regression risk — fix-commit co-change ranking

The system SHALL for a target file find fix commits (`is_fix=true`) touching it, retrieve other files changed in those commits, rank them by co-occurrence count and directional confidence `P(co-file|target)`, deduplicate commits and pairs, and return commit hashes, dates, messages, `fix_reason`, and ranked files as historical regression evidence, not a future-bug probability.

#### Scenario: Ranked co-changed files from fix commits

- **WHEN** fix commits `C1` and `C2` both modify `target.py` and `helper.py`, while only `C1` modifies `other.py`
- **THEN** regression risk for `target.py` ranks `helper.py` (count 2) above `other.py` (count 1) with evidence showing `C1` and `C2` hashes/dates/messages/reasons

#### Scenario: Deduplication of commits and pairs

- **WHEN** the same fix commit is discovered via multiple query paths
- **THEN** it appears once in evidence and contributes once to counts

#### Scenario: No fix commits yields no ranked files but preserves evidence language

- **WHEN** the target has no fix commits
- **THEN** the result has an empty ranked list, a limitation stating "no fix evidence found — does not indicate zero regression risk", and does not claim low risk

#### Scenario: Output is evidence, not probability

- **WHEN** regression risk is rendered for a consumer
- **THEN** the result text or description states it is "historical regression evidence" and does not phrase counts as a defect probability

### Requirement: Historical impact — importers, dependencies, co-change comparison

The system SHALL, for a target file, retrieve files that import the target (importers), files imported by the target (dependencies), and files that co-change with the target; compare historical `CO_CHANGES` pairs with current `IMPORTS` relationships; flag co-change pairs that have no direct import relationship in either direction as potential hidden couplings; and return relationship type, supporting commit evidence, and counts, without claiming a hidden coupling is a proven dependency.

#### Scenario: Importers and dependencies retrieved

- **WHEN** `A` imports `TARGET` and `TARGET` imports `B`
- **THEN** historical impact for `TARGET` lists `A` under `importers` and `B` under `dependencies` with relationship type `IMPORTS`

#### Scenario: Co-changed files compared to imports

- **WHEN** `TARGET` co-changes with `X` (count 5) and `X` has no `IMPORTS` edge to or from `TARGET`
- **THEN** `X` is listed under `co_changed` and flagged with `potential_hidden_coupling = true` with a note that it is an observed association, not a proven dependency

#### Scenario: Direct import suppresses hidden-coupling flag

- **WHEN** `TARGET` co-changes with `Y` and `Y` already has a direct `IMPORTS` edge to/from `TARGET`
- **THEN** `Y` is not flagged as a hidden coupling

#### Scenario: Evidence includes supporting commits

- **WHEN** co-change between `TARGET` and `Z` is derived from commits `C1` and `C3`
- **THEN** the result includes commit hashes/dates and co-change count/weight/confidence

### Requirement: Test recommendation ranking

The system SHALL rank candidate test files for a target file using up to four signals — test filename/naming conventions (`test_*.py`, `*_test.py`, `tests/**`, `*_tests.py`), tests that statically `IMPORTS` the target, historical co-change between target and test, and tests modified in past fix commits involving the target — and SHALL return each candidate with its rank, score, matching reasons, and supporting evidence, without claiming real test coverage or guaranteed behavioural relevance.

#### Scenario: Candidate ranked by multiple signals

- **WHEN** `tests/test_foo.py` imports `src/foo.py`, co-changed with it in 3 commits, and was touched in 1 fix commit involving `src/foo.py`
- **THEN** it appears near the top of the recommendation list with reasons `["naming_convention", "imports_target", "co_change:3", "fix_commit:1"]` and supporting evidence

#### Scenario: Naming convention alone is a valid signal

- **WHEN** `tests/test_bar.py` has naming similarity to `src/bar.py` but no import or co-change history
- **THEN** it still appears as a candidate (low rank) with reason `naming_convention` alone

#### Scenario: No claim of coverage

- **WHEN** test recommendations are returned
- **THEN** the result includes a disclaimer/limitation that returned tests are suggestions and do not represent measured coverage or guaranteed relevance

#### Scenario: Non-test files are not recommended as tests

- **WHEN** `src/helper.py` co-changes heavily with `target.py` but does not match any test-file heuristic
- **THEN** it is not returned in the test recommendation list

### Requirement: Missing history and confidence disclosure

The system SHALL distinguish insufficient evidence from zero risk and disclose coverage gaps in `limitations`. No fix commits, no co-change history, and no import edge SHALL each produce the documented insufficiency phrasing rather than a zero-risk or authoritative score. Shallow history, excluded paths, or parser failures affecting results SHALL be disclosed and may trigger `insufficient_history`.

#### Scenario: Empty fix history is not zero risk

- **WHEN** regression risk for a file has zero fix commits
- **THEN** the result does not contain phrasing like "low risk" or "no risk"; it contains "no fix evidence found" and notes the distinction from zero risk

#### Scenario: Missing co-change produces insufficient_history

- **WHEN** historical impact or criticality lacks co-change history for the file (e.g., newly added file with one commit)
- **THEN** status is `insufficient_history` or limitations state "insufficient co-change evidence" with commit-count context

#### Scenario: Missing import edge is not absence of dependency

- **WHEN** no `IMPORTS` edge exists between two files
- **THEN** limitations state "no static import relationship resolved — absence of an edge does not prove absence of a dependency (dynamic imports, see Iteration 4 limitations)"

#### Scenario: Shallow history disclosed

- **WHEN** the repository has fewer than the documented minimum commits (e.g., < 10 commits total) and criticality is requested
- **THEN** status is `insufficient_history` or limitations state "shallow history — N commits available, results have low confidence"

#### Scenario: Excluded paths and parser failures disclosed

- **WHEN** static-analysis diagnostics report that `k` files were excluded by filters or `p` files failed to parse, and a requested impact/fan-in result depends on imports
- **THEN** limitations lists the counts (`excluded_paths`, `parser_failures`) and notes that import-derived metrics may be incomplete

### Requirement: Determinism and idempotent reads

The system SHALL compute all analytics deterministically from the graph snapshot and validated configuration, with no random, time-of-query, or LLM-influenced variation; the same `(repository_id, target, graph state, decay_half_life_days, weights)` SHALL always produce identical scores, rankings, and evidence ordering.

#### Scenario: Repeated calls are identical

- **WHEN** the same analytics operation is invoked twice consecutively without graph or config changes
- **THEN** the two results are equal in every field including ordering of ranked lists

#### Scenario: Ordering is deterministic when scores tie

- **WHEN** two candidate files have equal recommendation scores
- **THEN** they are ordered by a documented tie-breaker (e.g., lexical path) so ordering is repeatable

### Requirement: Repository isolation for analytics reads

The system SHALL scope every analytics graph query by `repository_id` so that commits, files, and relationships from one repository never influence the scores, rankings, or evidence returned for another repository.

#### Scenario: Cross-repository isolation

- **WHEN** repositories `R1` and `R2` each contain a file `src/foo.py` with different histories
- **THEN** analytics for `R1:src/foo.py` returns only `R1` evidence and ignores `R2` data

### Requirement: Parameterized queries and read-only analytics

The system SHALL execute all analytics graph reads via parameterized Cypher (no string-interpolated file paths or repository IDs) and SHALL NOT mutate graph state; analytics SHALL be read-only and side-effect-free.

#### Scenario: Parameterized read

- **WHEN** analytics queries for file `a'; DROP` (injection-like path)
- **THEN** the query treats it as a literal path parameter and does not execute unintended Cypher

#### Scenario: No writes from analytics

- **WHEN** any analytics operation completes
- **THEN** the graph's node and relationship counts are unchanged

### Requirement: Fixed fixture produces stable results

The system SHALL ship or document a fixed graph fixture (seeded `Repository`, `File`, `Commit`, `CO_CHANGES`, `IMPORTS` data) and the analytics computed over that fixture SHALL match checked-in expected values (scores, breakdowns, rankings) so regressions are caught.

#### Scenario: Fixture regression check

- **WHEN** the fixture is loaded and all four analytics are run on its designated target files with the fixture's half-life
- **THEN** the computed scores, component breakdowns, and top-N rankings equal the expected values stored in `tests/fixtures/analytics/` within the documented tolerance

