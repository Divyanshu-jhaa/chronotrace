# Proposal: Iteration 5 — Deterministic Analytics and Evidence Contracts

## Why

ChronoTrace has a populated graph after Iterations 2–4 (stable `Repository`/`Commit`/`File` nodes, `CO_CHANGES` and `IMPORTS` relationships, decay config in `chronotrace.yml`), but no deterministic scoring or ranked evidence for its core product capabilities. Without a shared result contract, exponential time-decay, and the four analytics operations (change criticality, regression risk, historical impact, test recommendation) the MCP tools, agent, and API would have nothing authoritative to surface, and the LLM would be forced to invent numbers in violation of the evidence-first principle (PLAN §2.2).

Iteration 5 implements those analytics as pure Python over the graph layer, with no LLM involvement, so every score, ranking, and claim downstream can be traced to committed graph evidence with explicit insufficiency handling.

## What Changes

- **Common result contract** — Every analytics operation returns a typed result with: `status` (`ok` | `insufficient_history` | `not_found` | failure variant), `repository_id`, `target` (file or query scope), result values/rankings, `evidence` (commits, relationships, components used), and `limitations`/`data_coverage` (history depth, excluded paths, parser failures). Numeric results are kept separate from evidence-sufficiency; missing evidence is never presented as zero risk.
- **Exponential time decay** — Configurable decay using `analysis.history.decay_half_life_days` (Pydantic-validated positive number, from `chronotrace.yml`, default 365). Weight `w = 0.5 ^ (age_days / half_life)`; a commit one half-life old contributes half weight. Decay is applied consistently to every historical metric that uses recency (churn, fix involvement, co-change). Boundary cases (half-life zero/negative, future dates, age 0) are validated and tested.
- **Change Criticality** — Deterministic file-level score combining four evidence components: *decayed churn* (commit count weighted by decay + lines changed), *historical bug-fix involvement* (fix-labeled commits touching the file, decayed), *co-change degree / weighted co-change evidence* (number and strength of `CO_CHANGES` neighbors, decayed), and *static import fan-in* (count of importers via `IMPORTS`). Raw components are retrieved from the graph, normalized within-repository (documented min-max or z-score method), combined with explicit configurable/documented weights (initial weights marked heuristic until backtesting in Iteration 12), and returned with final score + per-component breakdown + source evidence (commits, relationship counts).
- **Regression Risk** — For a target file: find heuristic fix commits (`Commit.is_fix`) that modified the target; retrieve other files changed in those same fix commits; rank related files by co-occurrence count and directional confidence (`P(B|A)` where A=target change, B=co-changed file), deduplicating commits/relationships; return commit hashes, dates, messages, `fix_reason`, and ranked co-changed files. Output is framed as historical regression evidence, not a future-bug probability.
- **Historical Impact** — For a target file: retrieve `importers` (`<-IMPORTS-`), `dependencies` (`-IMPORTS->`), and `co-changed` files (`-CO_CHANGES-`); compare co-change pairs against import edges; flag co-change pairs with no direct import relationship as *potential hidden couplings* (observed association, not proven dependency); return relationship type, supporting commits, and counts.
- **Test Recommendation** — Rank candidate test files for a target using four signals: test filename/naming conventions (`test_*.py`, `*_test.py`, `tests/**`), tests that statically import the target, historical co-change between target and test, and tests modified in past fix commits involving the target. Each candidate is returned with its rank, score, and reasons; no claim of real coverage or guaranteed relevance.
- **Insufficient-history handling** — Explicit statuses and limitations: no fix commits → "no fix evidence found" (not zero risk); no co-change history → `insufficient_history` with reason; no resolved import edge → "no static relationship resolved, does not prove absence"; shallow history / excluded paths / parser failures disclosed when they affect results. Thresholds for "insufficient" are documented and tested.
- **Determinism and testability** — All operations are pure functions of `(graph snapshot, validated config)`; same inputs produce byte-identical scores. Full unit coverage for formulae (decay, normalization, weighting, ranking, deduplication) and a fixed graph fixture that produces stable expected results.

## Capabilities

### New Capabilities

- `analytics`: Deterministic analytics engine — common result contract (`status`, `repository_id`, `target`, `evidence`, `limitations`), exponential time decay, change criticality scoring (decayed churn + fix involvement + co-change + fan-in with normalization and weighted breakdown), regression risk (fix-commit co-change ranking with directional confidence), historical impact (importers/dependencies/co-change comparison and hidden-coupling flagging), test recommendation (naming + import + co-change + fix-history ranking), and insufficient-history/empty-evidence handling with explicit disclosure. Owns all score calculation and evidence-shaping logic consumed by MCP (Iteration 6) and API reports (Iteration 8).

### Modified Capabilities

- None — no existing shipped spec capability is modified. Iterations 0–4 established foundation, config, graph, ingestion, and static analysis; this iteration is additive. The `graph` capability is read (not changed) via new parameterized queries; `config` is read for `decay_half_life_days` and `batch_size`.

## Impact

- **New module**: `src/chronotrace/core/analytics/` — `contracts.py` (result dataclasses / status enum), `decay.py` (exponential decay, half-life handling), `criticality.py`, `regression.py`, `impact.py`, `test_reco.py`, `service.py` (facade orchestrating graph queries + scoring), optional `normalization.py`.
- **Graph layer**: New read-only repositories / parameterized Cypher in `src/chronotrace/core/graph/repositories/` for churn/commit history, fix-commit lookup, co-change counts with confidence, import fan-in/out, test discovery; reuses Iteration 2 driver lifecycle, session hygiene, repository isolation (`repository_id` scoping), and Iteration 3/4 identities.
- **Configuration**: Consumes `analysis.history.decay_half_life_days`, `graph.batch_size`, repository identity, exclusion diagnostics, parser-failure counts; no new secrets or YAML keys required (weights are code constants initially, configurable later).
- **Downstream**: Unblocks Iteration 6 (MCP tools `get_change_criticality`, `get_regression_risk`, `get_historical_impact`, `recommend_tests`, `get_file_history`) and Iteration 8 reports; agent iteration (7) can cite these deterministic outputs.
- **Dependencies**: Python standard library only for math (`math.exp` / `0.5 ** ratio`), `datetime` for age calculation; no new external packages.
- **Out of scope (explicitly excluded)**:
  - Any LLM, embedding, or heuristic score invention — analytics is deterministic only (PLAN §2.2).
  - SZZ bug-inducing commit identification, function/symbol-level tracking, real per-test coverage (PLAN §7 exclusions).
  - Backtesting evaluation of criticality weights (Iteration 12).
  - MCP/server, agent, API, CLI, UI, Docker work (Iterations 6–11).
  - Mutating writes — analytics is read-only over the existing graph.

## Acceptance Criteria (Iteration-level)

- See PLAN.md Iteration 5 — scope, common contract, time-decay, per-capability requirements, missing-history policy, and completion gate apply; summarized here for traceability:
  - All four capabilities work without an LLM and are deterministic for the same graph + config.
  - Every score has a component breakdown; every factual result includes traceable evidence (hashes, dates, counts, reasons).
  - Decay uses configured half-life, half-weight at one half-life, consistently applied and boundary-tested.
  - Insufficient-history / not-found / shallow-history / excluded-path / parser-failure cases produce explicit `status` and `limitations`, never a false zero-risk claim.
  - Unit tests cover formulae, ranking, normalization, missing data, duplicate evidence, and date handling; a fixed graph fixture produces stable expected results.
  - Completion gate: all four operations invokable directly in Python returning documented result objects with evidence.

## Dependencies and Sequencing

- Depends on: Iteration 1 (validated `decay_half_life_days`, `repositories_dir`, `batch_size`), Iteration 2 (graph schema, stable `Repository`/`File`/`Commit` IDs, `CO_CHANGES` relationship with counts/weights, isolation, parameterized Cypher), Iteration 3 (commit/file history, `is_fix`/`fix_reason`, `REVERTS`), Iteration 4 (`IMPORTS` graph, diagnostics).
- Unblocks: Iteration 6 (MCP tool contracts), Iteration 8 (report endpoint), Iteration 12 (backtesting over criticality weights).
