# Proposal: Iteration 11 — Web UI and Graph/Report Presentation

## Why

ChronoTrace has a complete backend stack after Iterations 0–10 (validated config, Neo4j graph, ingestion, static analysis, deterministic analytics, MCP tools, agent, FastAPI API, Docker Compose, and CLI), but no browser interface for demonstration. Without a minimal web UI, users cannot visually select repositories, chat with evidence-grounded answers, inspect deterministic reports, or explore file relationships without using the CLI or direct API calls, limiting hackathon demo effectiveness.

Iteration 11 adds a lightweight web UI that communicates only through the existing API, presents deterministic analytics with full evidence/limitations disclosure, and visualizes the file relationship graph, completing the user-visible surface before hardening in Iteration 12.

## What Changes

- **Repository selection** — Web UI lists ingested repositories (from API repository listing/status endpoint), lets the user select an active repository, and persists the selection for subsequent chat/report/graph requests. Empty state when no repositories are ingested.
- **Chat view** — Input for repository-scoped questions, display of user/assistant messages, streaming or polling rendering of agent responses, evidence citations (paths, commit hashes, dates, counts, scores) inline, and explicit insufficient-evidence / error states. No direct Neo4j or MCP access from the browser.
- **Deterministic report view** — File-scoped report panel that calls the API report endpoint (no LLM) and renders: change criticality score with per-component breakdown (decayed churn, fix involvement, co-change, fan-in, normalized/weighted values), regression risk ranked co-changed files with fix-commit evidence (hashes, dates, messages, fix_reason, counts, directional confidence), historical impact (importers, dependencies, co-changed sets, hidden-coupling flags with "observed association, not proven dependency" framing), and test recommendations (candidates with reasons: naming, imports, co-change, fix history, plus coverage disclaimer).
- **Graph visualization** — Simple interactive visualization for a selected file and its relationships (importers, dependencies, co-changed neighbors, hidden couplings distinguished). Data sourced exclusively from the API (which sources from deterministic analytics/graph layer); UI does not compute scores. Supports selection/hover to reveal evidence details (relationship type, count, weight, confidence).
- **Evidence and limitations display** — Every view surfaces structured evidence (paths, commit hashes, dates, relationship types, counts, scores) and limitations/insufficient-history states (shallow history, no fix evidence, missing import edge disclaimer, excluded-path/parser-failure diagnostics) exactly as returned by the API/analytics contract.
- **API-only communication** — All browser data flows through the FastAPI backend (`/health`, `/repositories`, `/chat`, `/reports`, `/graph` or equivalent). No Neo4j credentials, Bolt connections, or secrets exposed to or stored in the browser. CORS and base-URL configuration handled for local dev (`http://localhost:<api-port>`) and Docker Compose networking.
- **UI shell and error handling** — Lightweight frontend (static assets under `web/` served via FastAPI static mount or standalone dev server), responsive layout for chat/report/graph panels, loading/empty/error states, and actionable messages for API unavailability or ingestion-in-progress.
- **Separation of concerns** — UI is presentation-only; analytics scoring, ranking, and evidence shaping remain in `core/analytics` and API services. No duplicated scoring logic in frontend code.

## Capabilities

### New Capabilities

- `web-ui`: Browser-based presentation layer for ChronoTrace — repository selection from ingested repositories, chat view for evidence-grounded Q&A, deterministic report view (criticality with breakdown, regression evidence, historical impact with hidden-coupling flags, test recommendations with reasons), graph visualization for file relationships (importers/dependencies/co-change/hidden couplings), and evidence/limitations/insufficient-history rendering. All data via the API; no direct graph or credential access; presentation-only with explicit empty/error/loading states.

### Modified Capabilities

- None — no existing shipped spec capability requirements are changed. This iteration is additive. The `api` capability (Iteration 8) is consumed but not modified at the spec level; if an API endpoint shape needs extension for report/graph payloads, it is an implementation detail of the existing API, not a requirement change in this change.

## Impact

- **New code**: `web/` directory — static frontend assets (HTML/CSS/JS, or a minimal framework such as Vite + vanilla JS/React as appropriate; no heavy build-chain commitment in proposal), `src/chronotrace/api/static.py` or `web` static mount in FastAPI, frontend API client module. No new Python backend dependencies required beyond existing FastAPI static-files support (`starlette.staticfiles` or `fastapi.staticfiles`).
- **Modified code**: `src/chronotrace/api/main.py` — mount static files and/or add `GET /` serving of `web/index.html`; optional CORS middleware configuration for local dev. `docker-compose.yml` — optional `web` service or serving via `agent-api` container; no new Neo4j wiring.
- **APIs / Systems**: Consumes existing FastAPI endpoints (repositories, chat, reports, health/readiness); no new MCP tools or graph schema changes. If the report/graph payloads need new fields (e.g., graph adjacency list), they are added as backward-compatible API response extensions, not breaking changes.
- **Dependencies**: Browser `fetch` API; optional lightweight graph library (e.g., D3, Cytoscape.js, or vis-network) for visualization — decision deferred to design, kept minimal to preserve demo reliability.
- **Breaking changes**: None.
- **Out of scope (explicitly excluded)**:
  - Timeline slider UI, CI/flaky-test history, ownership/reviewer recommendations (PLAN §7).
  - SZZ, function/symbol-level tracking, real per-test coverage, embeddings/vector search, multi-language parsing.
  - LLM-assisted fix labeling, Muse Wonder deep-analysis mode, GitHub Action/PR bot.
  - Local inference (Muse Glimmer), web-search grounding for changelogs.
  - Authentication, multi-user, or role-based access — single-user demo UI.
  - Offline/PWA support, server-side rendering (SSR) framework commitment.

## Acceptance Criteria (Iteration-level)

- See PLAN.md Iteration 11 — scope and completion gate apply; summarized here for traceability:
  - User can select an ingested repository and the selection scopes subsequent requests.
  - User can submit a chat question and view the evidence-grounded response with citations.
  - User can inspect a deterministic report (criticality breakdown, regression evidence, impact candidates, test recommendations with reasons).
  - Graph view displays relationships returned by the API (importers/dependencies/co-change/hidden couplings) and reveals evidence on interaction.
  - Evidence (paths, hashes, dates, types, counts) and limitations/insufficient-history states are visible in every view.
  - Empty and error states are handled without crashing.
  - UI does not calculate scores independently; no Neo4j credentials or direct DB connections in browser code.

## Dependencies and Sequencing

- Depends on: Iteration 1 (validated config, API base URL), Iteration 2 (graph schema for reads), Iteration 5 (analytics contracts with status/evidence/limitations), Iteration 6 (MCP tools — not directly consumed by UI but validates analytics), Iteration 7 (agent for chat), Iteration 8 (FastAPI endpoints for chat/reports/repositories/health), Iteration 9 (Docker Compose networking and published API port), Iteration 10 (CLI parity — UI is an alternative presentation, not a replacement).
- Unblocks: Iteration 12 (hardening) — UI provides an additional surface for unit/integration/E2E testing, security review (no credential exposure), and demo reliability validation.
