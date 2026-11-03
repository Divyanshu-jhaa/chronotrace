# Spec Delta

## Purpose

Defines a reproducible backtest methodology that measures whether ChronoTrace rankings carried predictive signal about future fix activity, compared honestly against a simple baseline.

## ADDED Requirements

### Requirement: Backtest dataset and evaluation protocol

The system SHALL document a backtest that selects a demo Python repository with meaningful history and tests, defines historical evaluation points (commits or dates), and for each point computes rankings using only history available strictly before that point.

#### Scenario: Evaluation points use only prior history

- **WHEN** the backtest computes a ranking for an evaluation point at time T
- **THEN** only commits, files, and relationships with dates before T are ingested into the graph used for that ranking; commits at or after T are excluded

#### Scenario: Demo repository and time window are documented

- **WHEN** a reviewer reads the backtest report
- **THEN** the report names the repository URL, the commit range or date window evaluated, and the reason the repository was chosen (history depth, test presence, Python scope)

### Requirement: Measurable target and metric

The system SHALL define a measurable target for the backtest, such as whether files ranked highly at an evaluation point were involved in subsequent fix commits within a stated time window after that point, and SHALL report the metric, its calculation, sample size, and limitations without overstating predictive validity.

#### Scenario: Target is stated and measurable

- **WHEN** the backtest report is inspected
- **THEN** it states the target (e.g., "top-K ranked files appearing in fix commits within N days"), the metric (e.g., precision@K, hit rate, or rank correlation), and the window length used for the future observation

#### Scenario: Results disclose sample size and limitations

- **WHEN** the backtest completes, even with a small or biased sample
- **THEN** the report includes the number of evaluation points, the total file/commit counts, and a limitations section that explicitly notes the sample is not claimed as general predictive validity

### Requirement: Baseline comparison

The system SHALL compare ChronoTrace's backtest result against a simple baseline such as raw churn (change count) or bug-fix involvement count computed from the same prior history, using the same evaluation points and target window.

#### Scenario: Baseline uses same history slice

- **WHEN** the backtest computes the baseline ranking for an evaluation point
- **THEN** the baseline is derived from the identical pre-T history slice as the ChronoTrace ranking, not from full history or a different window

#### Scenario: Comparison is reported fairly

- **WHEN** the backtest report compares ChronoTrace to the baseline
- **THEN** it shows both results side by side (e.g., precision@K for each) and does not cherry-pick a single favorable point to represent overall performance

### Requirement: Single honest headline metric

The system SHALL produce one honest headline metric for the demo that summarizes the backtest outcome (e.g., "precision@10 = X% vs baseline Y% over N evaluation points"), and SHALL qualify it with the dataset and window so it cannot be mistaken for a general claim.

#### Scenario: Headline metric is reproducible

- **WHEN** the backtest is re-run from a clean environment using the documented command (e.g., `make backtest` or `python scripts/backtest.py --config ...`)
- **THEN** it produces the same headline metric within documented tolerance and writes the full report to a known path (e.g., `docs/backtest.md` or `reports/backtest.json`)

### Requirement: Backtest reproducibility and documentation

The system SHALL make the backtest reproducible by documenting the exact command, required inputs (repository URL, commit range, config, decay half-life used), output artifacts, and prerequisites, and SHALL store the report under version control or as a generated artifact referenced in documentation.

#### Scenario: Clean re-run reproduces the report

- **WHEN** a reviewer follows the documented steps from a clean checkout on a machine with Docker and Neo4j available
- **THEN** the backtest completes, writes its report, and the report's dataset, window, sample size, metric definitions, and limitations sections are populated rather than left as placeholders

#### Scenario: Tiny or biased sample is not overstated

- **WHEN** the backtest dataset is small or skewed (e.g., fewer than a threshold of evaluation points or a narrow time window)
- **THEN** the report labels the result as exploratory and does not claim predictive validity, in line with the "do not claim predictive validity from a tiny or biased sample" constraint
