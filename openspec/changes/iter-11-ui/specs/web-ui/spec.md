# Spec Delta

## Purpose

Provides the browser-based demo interface for ChronoTrace so users can select ingested repositories, chat with evidence-grounded answers, inspect deterministic reports with full breakdowns, and explore file-relationship graphs without direct database access.

## ADDED Requirements

### Requirement: Repository selection

The system SHALL provide a repository-selection view that lists ingested repositories obtained from the API and allows the user to select one active repository whose identifier scopes all subsequent chat, report, and graph requests.

#### Scenario: List ingested repositories

- **WHEN** the UI loads and the API returns a list of repositories with identifiers, names, ingestion status, and head commit metadata
- **THEN** the UI renders the list showing repository name/identifier and status, with at least one selectable entry when repositories exist

#### Scenario: Select active repository

- **WHEN** the user selects a repository from the list
- **THEN** the UI marks it as active, persists the selection for the session (in-memory or local storage), and all subsequent chat, report, and graph requests include that repository identifier

#### Scenario: Empty state when no repositories ingested

- **WHEN** the API returns an empty repository list
- **THEN** the UI shows an empty-state message explaining no repositories are ingested and directs the user to the CLI `chronotrace add <git-url>` flow, without rendering a broken list

#### Scenario: Repository selection failure is surfaced

- **WHEN** the repository-listing API call fails (network error or 5xx)
- **THEN** the UI shows an actionable error message with retry capability and does not leave the list in a stale loading state

### Requirement: Chat view

The system SHALL provide a chat view that lets the user submit repository-scoped questions to the API chat endpoint and renders the conversation (user messages, assistant responses, evidence citations, and limitation notices) with loading, streaming, and error states.

#### Scenario: Submit chat question and view response

- **WHEN** the user selects a repository, types a question, and submits it
- **THEN** the UI sends the question with the active repository identifier to the API chat endpoint and renders both the user message and the returned assistant response in the conversation view

#### Scenario: Streaming or progressive rendering when supported

- **WHEN** the API streams the chat response (SSE or chunked JSON) or returns a complete response after tool iteration
- **THEN** the UI progressively displays or finally displays the full assistant output without requiring a page reload, preserving message order

#### Scenario: Evidence citations visible in chat responses

- **WHEN** the assistant response includes evidence from tool output (file paths, commit hashes, dates, counts, scores)
- **THEN** the UI renders that evidence inline or in an evidence panel attached to the message and does not strip or reformat it in a way that loses traceability

#### Scenario: Insufficient-evidence disclosure in chat

- **WHEN** the API returns a chat response that notes insufficient history, missing evidence, or limitations (propagated from analytics statuses)
- **THEN** the UI displays that limitation text verbatim or faithfully paraphrased with clear visual distinction, never as a false zero-risk claim

#### Scenario: Chat error handling

- **WHEN** the chat request fails (API error, model unavailable, timeout)
- **THEN** the UI shows an actionable error message explaining the failure (e.g., model unavailable, try again), preserves the conversation history, and does not fabricate an answer

#### Scenario: Chat without selected repository is prevented

- **WHEN** no repository is selected and the user attempts to submit a chat message
- **THEN** the UI prevents the submission and prompts the user to select a repository first

### Requirement: Deterministic report view

The system SHALL provide a deterministic report view that retrieves a file-scoped report from the API report endpoint (no LLM) and renders change criticality with component breakdown, regression evidence, historical impact candidates, and recommended tests with reasons, alongside evidence and limitations.

#### Scenario: Request and display deterministic report

- **WHEN** the user selects a repository and enters a target file path and requests a report
- **THEN** the UI calls the API report endpoint with repository identifier and canonical file path and renders the returned report sections (criticality, regression, impact, test recommendations) with their evidence and limitations

#### Scenario: Criticality score with component breakdown

- **WHEN** the report's criticality payload contains `score` and per-component breakdown (`churn`, `fix_involvement`, `cochange`, `fan_in` with raw, normalized, weighted values) and weights documentation
- **THEN** the UI displays the overall score numerically (e.g., 0–1 scale) and each component's raw/normalized/weighted contribution in a breakdown table or chart, with weights labeled as heuristic where applicable

#### Scenario: Regression evidence rendered

- **WHEN** the report's regression payload lists fix commits (hash, date, message, fix_reason) and ranked co-changed files (path, co-occurrence count, directional confidence)
- **THEN** the UI lists the fix commits with their metadata and the ranked files with counts and confidence values, labeled as historical regression evidence, not a future-bug probability

#### Scenario: Historical impact with hidden-coupling flags

- **WHEN** the report's impact payload contains importers, dependencies, co-changed files, and hidden-coupling flags (`potential_hidden_coupling` with note text)
- **THEN** the UI displays the three relationship sets separately and visually distinguishes flagged hidden couplings with the "observed association, not proven dependency" framing and supporting commit evidence and counts

#### Scenario: Test recommendations with reasons

- **WHEN** the report's test-recommendation payload contains ranked candidates with `reasons` (naming_convention, imports_target, co_change count, fix_commit count) and evidence
- **THEN** the UI lists candidates in ranked order with their scores and reason tags, and includes a disclaimer that suggestions are heuristic and do not represent measured coverage or guaranteed relevance

#### Scenario: Report insufficient-history states

- **WHEN** a report returns `status=insufficient_history` or `not_found` or includes limitations (shallow history, no fix evidence, no static import edge, excluded paths, parser failures)
- **THEN** the UI renders the status and limitation text prominently (e.g., "no fix evidence found — does not mean zero risk", "no static import — absence does not prove absence of dependency") and does not present a false zero-risk or authoritative score

#### Scenario: Report path validation

- **WHEN** the user enters an empty or malformed file path and requests a report
- **THEN** the UI validates the input client-side, shows an actionable validation message, and does not call the API with an invalid path

### Requirement: Graph visualization

The system SHALL provide a simple graph visualization for a selected file that renders its file-relationship neighborhood (importers, dependencies, co-changed neighbors, hidden couplings) from data returned by the API, with interactive evidence inspection and clear relationship-type distinction.

#### Scenario: Render graph from API data

- **WHEN** the user selects a file (via report or a file picker) and requests its graph view
- **THEN** the UI fetches relationship data (importers `IMPORTS <-`, dependencies `IMPORTS ->`, `CO_CHANGES` neighbors with counts/weights/confidence, hidden-coupling flags) from the API and renders nodes (files) and edges (typed relationships) in a canvas/SVG visualization

#### Scenario: Graph distinguishes relationship types

- **WHEN** the graph contains mixed edge types (import vs co-change vs hidden coupling)
- **THEN** the UI visually distinguishes them (different color, style, or label) and provides a legend so the user can tell imports from co-change associations

#### Scenario: Graph interaction reveals evidence

- **WHEN** the user hovers or selects a node or edge in the graph
- **THEN** the UI shows a detail panel or tooltip with evidence: file path, relationship type, count, weight, confidence, and supporting commit references where available

#### Scenario: Graph uses only API data and does not compute scores

- **WHEN** the graph view is rendered
- **THEN** the graph's nodes, edges, counts, and hidden-coupling flags are exactly those returned by the API; the UI does not independently calculate scores, co-change counts, or confidences, and does not invent edges

#### Scenario: Graph empty or insufficient states

- **WHEN** the selected file has no relationships (isolated file) or the API returns `insufficient_history` / `not_found`
- **THEN** the UI shows an informative empty-state message describing the lack of relationship evidence (with limitation text) rather than a blank or broken visualization

### Requirement: Evidence and limitations display

The system SHALL display structured evidence and limitations consistently across chat, report, and graph views, preserving the values returned by the API/analytics contracts without silent truncation or reinterpretation.

#### Scenario: Evidence fields are visible

- **WHEN** any view receives API data containing evidence fields (file paths, commit hashes (full or short), dates, relationship types, counts, weights, confidence values, scores, fix reasons)
- **THEN** the UI renders those values in a readable, copyable form so a user can trace claims back to graph evidence

#### Scenario: Limitations and warnings are prominent

- **WHEN** the API payload includes `limitations` or `warnings` (shallow history with commit counts, excluded-path counts, parser-failure counts, "no fix evidence" note, "absence of edge does not prove absence of dependency" note)
- **THEN** the UI renders them in a dedicated limitations section or callout with the exact or faithfully preserved phrasing, visually distinct from the main result

#### Scenario: Deterministic report available without LLM

- **WHEN** the model provider is unavailable but the API report endpoint is reachable
- **THEN** the UI still renders the deterministic report correctly, and the chat view shows a clear model-unavailable message without affecting report functionality

### Requirement: Presentation-only UI with API-only communication

The system SHALL keep all UI presentation separate from analytics logic and communicate with the backend only through the API over HTTP(S), without direct Neo4j connections, MCP calls, or credential exposure in the browser.

#### Scenario: UI does not calculate scores

- **WHEN** criticality, regression, impact, or test-recommendation scores are displayed
- **THEN** the values shown equal the API's computed values; the frontend code does not contain scoring, ranking, or decay formulae, and stores no graph-derived computation outside display logic

#### Scenario: No Neo4j credentials or direct DB access from browser

- **WHEN** browser network traffic and bundle sources are inspected
- **THEN** no Neo4j Bolt URI, username, password, or raw Cypher queries appear in requests, source, or storage, and all data fetches target only the configured API base URL

#### Scenario: Configurable API base URL and CORS

- **WHEN** the API base URL is configured (environment variable, `chronotrace.yml` via API, or UI config) and the browser is at `http://localhost:<ui-port>` while the API is at `http://localhost:<api-port>` or `agent-api:8000` via Compose
- **THEN** requests target the configured API base URL with correct CORS handling, and a misconfigured base URL produces an actionable connection-error message

#### Scenario: No secrets in browser storage

- **WHEN** browser storage (localStorage, sessionStorage, cookies) is inspected during normal use
- **THEN** no model API keys, Neo4j credentials, or other secrets are persisted client-side

### Requirement: UI shell, navigation, and error handling

The system SHALL provide a navigable UI shell with distinct sections for repository selection, chat, deterministic reports, and graph visualization, plus consistent loading, empty, and error states.

#### Scenario: Navigation between views

- **WHEN** the user navigates between repository selection, chat, report, and graph views (tabs, sidebar, or equivalent)
- **THEN** the active repository selection persists, and each view loads correctly without a full page reset losing the selection

#### Scenario: Loading states

- **WHEN** an API call (repository list, chat, report, graph) is in flight
- **THEN** the UI shows a loading indicator for that view and does not show stale data as current

#### Scenario: Global error handling for API unavailability

- **WHEN** the API is unreachable (network error, 502/503, CORS failure)
- **THEN** the UI shows a global or view-scoped error message explaining the API is unavailable, with a retry affordance, and does not render partial or inconsistent data as success

#### Scenario: Responsive display of empty results

- **WHEN** a view legitimately has no data (e.g., file with no importers, no co-change neighbors, no test candidates)
- **THEN** the UI shows a contextual empty-state explanation ("no importers resolved", "no co-change neighbors found") rather than omitting the section

#### Scenario: UI assets are servable locally and via Docker

- **WHEN** the UI is served via FastAPI static mount or standalone dev server, locally (`pip install -e .`) and when deployed with `docker compose up` (same backend image's `agent-api` service)
- **THEN** `GET /` or the configured UI path returns the UI's entry HTML, and static assets (JS/CSS) load without 404, verified by `curl -f http://localhost:<port>/` during smoke tests
