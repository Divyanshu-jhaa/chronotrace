# Design: Iteration 11 — Web UI and Graph/Report Presentation

## Context

After Iterations 0–10 ChronoTrace has: an installable `src`-layout package (`chronotrace` with `pyproject.toml` / `typer` CLI), Pydantic-validated `chronotrace.yml` (Iter 1) with `graph`/`agent`/`analysis` sections, a Neo4j graph layer with repository-scoped IDs and parameterized Cypher (Iter 2), Git ingestion via PyDriller (Iter 3), AST import graph (Iter 4), deterministic analytics with `status`/`evidence`/`limitations` contracts (Iter 5), MCP tools (Iter 6), the Muse Spark agent loop (Iter 7), a FastAPI `agent-api` service with `POST /chat`, `POST /reports`, `GET /repositories`, and `GET /health|/ready` (Iter 8), a single backend image for `mcp-server` and `agent-api` plus Compose networking (Iter 9), and the CLI commands `init/up/down/status/add/chat/report/version` (Iter 10). There is no browser UI — the only demo paths are CLI and raw HTTP.

Iteration 11 adds a minimal demo UI that is presentation-only, drives the existing API, and surfaces evidence and limitations faithfully. Constraints: Python 3.12+, FastAPI already in `pyproject.toml` with no new backend dependencies required, Docker Compose hosts only `neo4j`/`mcp-server`/`agent-api` by default, PLAN §7 excludes advanced UI features (timeline slider, multi-user auth, etc.), and PLAN §2.2/§5 requires Neo4j parameterized access and no credential leakage.

See `proposal.md` — Why/What Changes for motivation and `specs/web-ui/spec.md` — for normative behavior; this doc covers *how*.

## Goals / Non-Goals

**Goals:**

- Provide a shippable, locally servable web UI that lets a demo audience select a repository, chat, inspect deterministic reports with breakdowns, and see relationships graphically, all without direct DB access.
- Keep the UI reliably demo-able: deterministic load, visible evidence/limitations, robust empty/error/loading states, and no duplicated scoring logic.
- Serve the UI from the existing `agent-api` image (static mount) so `docker compose up` remains the only user-facing deploy step; support a standalone dev server for frontend iteration without Docker.
- Establish a testable frontend contract (API client, view components, state) that Iteration 12 can cover with unit/integration/E2E tests.

**Non-Goals:**

- Rich SPA framework commitment, SSR, offline/PWA, authentication, multi-user, role-based access (PLAN excludes; single-user demo).
- Timeline slider, CI/flaky-test history, ownership/reviewer recommendations, SZZ, function-level tracking, real coverage, embeddings, multi-language (PLAN §7 exclusions).
- New graph writes, new Cypher, new MCP tools, or new analytics calculations — UI is read-only presentation.
- Caching/memoization layer for reports or chat; can be added compatibly later.
- Theming engine or full design system — minimal, clean, demo-grade styling is sufficient.

## Decisions

### Decision 1 — Serving model: FastAPI static mount + optional dev server

**Choice:**

- Production / Compose: FastAPI mounts `web/` as static files (`from fastapi.staticfiles import StaticFiles; app.mount("/ui", StaticFiles(directory="web/dist"), name="web")` or serves `web/index.html` at `GET /`). The backend Dockerfile copies the pre-built `web/dist` (or plain `web/` for vanilla assets) into the image. No new Compose service is required; `agent-api` serves both API and UI.
- Development: `web/` can be served by a tiny dev server (`python -m http.server` or `vite` dev) that proxies API to `http://localhost:8000` (or the configured `agent.api_base_url`). The choice is documented but not enforced in code.

**Rationale:** PLAN's container strategy already uses one backend image for two services; adding a fourth `web` service adds networking, CORS, and volume complexity for no functional gain. Static mount keeps deployment to `docker compose up` and preserves the existing published API port as the only host-exposed endpoint. It also avoids committing to a heavy frontend toolchain for a minimal demo UI; even vanilla HTML/CSS/JS satisfies the spec.

**Alternatives considered:**

- Separate `web` Compose service (Node/Nginx) serving the SPA and proxying API — rejected: extra image, extra port, extra failure mode; justified only if the UI grows into a full product SPA, which PLAN defers.
- Electron / Tauri desktop app — rejected: out of scope for a browser demo and adds packaging burden.

### Decision 2 — Frontend stack: vanilla JS + lightweight graph library (no heavy framework lock-in)

**Choice:**

- Base: vanilla HTML/CSS/JS under `web/` (`index.html`, `styles.css`, `app.js` plus `lib/api.js`, `lib/store.js`, `components/*.js`), or equivalently a minimal Vite + preact/lit build if the team prefers — either satisfies the spec, so the design allows both provided `web/dist` is the build artifact. Recommendation is vanilla JS for demo simplicity and to avoid a Node toolchain requirement in the backend image build.
- Graph: one lightweight client library for node-edge rendering — `vis-network` or `cytoscape.js` (or D3 force layout if already familiar). Selection is deferred to implementation; any suffices as long as it can render typed edges, support hover/selection, and stay under ~200 kB gzipped. A plain SVG/Canvas fallback with manual layout is acceptable if the library choice stalls.
- Styling: minimal CSS (or a tiny utility layer like `water.css` / hand-written flex/grid) — no design-system commitment; accessible contrast, responsive panels, and copyable code blocks are enough.

**Rationale:** A demo UI must be reliable, small, and understandable by the hackathon team after Iteration 12. A heavy framework (React/Next/Vue with SSR) adds build, routing, and state-management overhead that the iteration's scope does not need. Vanilla JS keeps the bundle trivial, avoids transpilation in the Docker build when desired, and is fully testable with Playwright/E2E.

**Alternatives considered:**

- React + Vite SPA — viable but deferred as over-engineering for the current scope; can be adopted later by replacing `web/` without changing the API contract.
- No graph library (pure SVG manual layout) — kept as fallback if the chosen library proves flaky in Docker.

### Decision 3 — API surface consumed (no new endpoints unless strictly needed)

**Choice (consume existing Iter 8 endpoints, extend only if analytics payload already requires it):**

| UI need | HTTP call | Notes |
|---|---|---|
| List / status of repos | `GET /repositories` or `GET /repos` (as shipped in Iter 8) | Iter 8 already exposes "repository listing/status endpoint if required by the CLI" — reuse verbatim |
| Chat | `POST /chat` `{ repository_id, message, history? }` | Returns `{ answer, evidence, limitations, tool_calls? }` as in agent iteration |
| Deterministic report | `POST /reports` or `POST /reports/{repo}/file` `{ repository_id, file_path }` | Iter 8: "report endpoint ... can work without an LLM" — reuse. If Iter 8 shipped `GET /reports?repo=&file=`, adapt; UI client abstracts the path |
| Graph adjacency | Reuse report's `impact` payload (importers/dependencies/co-change/hidden couplings) for the graph | If a dedicated `GET /graph?repo=&file=` is useful, add it as a thin alias over `AnalyticsService.historical_impact` — not a spec requirement, so treated as an optional additive endpoint |

No new MCP calls from the browser; no WebSocket unless chat streaming is already shipped (SSE fallback otherwise). Mutations remain `POST /repositories` (ingestion) via CLI only; UI is read-oriented.

**Rationale:** PLAN Iter 11 explicitly says "Communicate only through the API" and "Keep UI presentation separate from analytics logic." Reusing Iter 8's endpoints enforces that separation and avoids drifting the API.

### Decision 4 — State and API client separation

**Choice:**

```
web/
  index.html
  styles.css
  app.js               # router/shell: tabs (Repos | Chat | Report | Graph), activeRepo state
  lib/
    api.js             # fetch wrapper: baseUrl from <meta> or window.__CHRONOTRACE_CONFIG__, typed helpers: listRepos(), chat(repoId, msg), getReport(repoId, path), getGraph(repoId, path)
    store.js           # tiny observable store: activeRepositoryId, repositories[], chatHistory[], reportCache, graphCache; localStorage persistence for active repo only (no secrets)
  components/
    repo-list.js
    chat-view.js
    report-view.js
    graph-view.js
    evidence-panel.js  # shared evidence/limitations renderer
```

- `api.js` is the only module that calls `fetch`; all requests include `Content-Type: application/json` and handle `ok/error/insufficient_history/not_found` uniformly, mapping HTTP errors to user messages.
- `store.js` holds in-memory state; only `activeRepositoryId` is persisted to `localStorage` (no secrets, no chat history persistence by default).
- Components are functions that take `(containerEl, store, api)` and render via `innerHTML` or DOM helpers — no framework reactivity needed.

**Rationale:** Matches the CLI decomposition (Iter 10: `api_client.py` separated from commands) and the analytics `service.py` facade pattern. A tiny explicit store is trivially testable and debuggable in the browser console.

**Alternatives considered:**

- Redux/Zustand — rejected as overkill for four views and one shared ID.

### Decision 5 — Evidence and limitations rendering contract

**Choice:** A shared `evidence-panel.js` renders the `evidence` + `limitations` contract identically across Chat, Report, and Graph:

- **Evidence** — copyable `<code>` blocks for paths, hashes (shown full or `shortHash (full on hover/copy)`), formatted dates (`YYYY-MM-DD` + `toLocaleString` tooltip), relationship type badge, counts / confidence as plain numbers with 2-decimal formatting where needed, scores with fixed precision.
- **Limitations** — amber callout with icon and the API's phrasing preserved: "no fix evidence found — does not mean zero risk", "no static import relationship resolved — absence does not prove absence of a dependency", shallow-history warning with `commit_count`, excluded/parse-failure counts.
- **Graph** — node label is `basename(path)` with full path on hover; edge label shows `type` and `count`/`confidence`; hidden couplings get a dashed edge and warning badge.

**Rationale:** PLAN §2.1 and the analytics contract require that limitations travel with results and are never softened to zero-risk. A single renderer guarantees consistency and prevents chat/report/graph from diverging in phrasing.

### Decision 6 — Base-URL configuration and CORS

**Choice:**

- The API base URL is resolved at runtime: `window.__CHRONOTRACE_CONFIG__.apiBaseUrl` injected by FastAPI into `index.html` (template replace at startup from validated `agent.api_base_url` or env), falling back to `window.location.origin` (same-origin when static-mounted) and then to `http://localhost:8000`.
- FastAPI adds `CORSMiddleware` (`allow_origins=["*"]` for local demo or `allow_origins` from config; `allow_methods=["GET","POST"]`, `allow_headers=["*"]`) so a standalone `web/` dev server on a different port works. Credentials are not used (`allow_credentials=False`), so wildcard origin is acceptable for the MVP.

**Rationale:** Keeps browser config API-only — no `.env` leakage — and handles both same-origin (static mount) and cross-origin (dev server, Compose) cases. The existing Iter 1 config already validates the API URL.

**Alternatives considered:**

- Build-time base-URL injection (bundler `VITE_API_URL`) — rejected: couples build to deploy host; runtime injection is simpler for the static mount.

### Decision 7 — Graph data flow and visualization approach

**Choice:**

- Fetch: `getGraph(repoId, path)` (or reuse `getReport`) returns `{ nodes: [{ id: path, is_target }], edges: [{ source, target, type: "IMPORTS"|"CO_CHANGES", count, weight, confidence, potential_hidden_coupling }] }`. The API constructs this from `AnalyticsService.historical_impact` plus file identity; no new graph queries are added.
- Render: force-directed layout (vis-network's physics or d3-force) with target node centered/emphasized, importers/dependencies/co-change clusters visually separated, hidden couplings dashed. Interaction: click selects node/edge and populates `evidence-panel` with its metadata; zoom/pan are library defaults.
- Scale guard: limit graph to `N <= 50` neighbors (configurable `limit` query param, default 30) ordered by `count`/`confidence` desc; if truncated, show "showing top N of M" limitation note. Prevents layout thrashing on hub files with 500+ neighbors.

**Rationale:** Reuses the deterministic `CO_CHANGES` / `IMPORTS` evidence already available; bounding the neighborhood keeps the demo smooth without pagination complexity.

### Decision 8 — Error, empty, and loading contracts

**Choice:**

- Every view has three states: `loading` (spinner + "Loading…"), `empty` (illustrated message + CTA, e.g., "No repositories yet — run `chronotrace add <url>`"), `error` (banner with HTTP status / message + Retry button that re-invokes the same `api.*` call). States are mutually exclusive per view; a global toast is optional.
- Chat adds a fourth `streaming` micro-state (typing indicator) if streaming is supported; otherwise the assistant bubble appears atomically after the response.
- HTTP mapping: `404 → "Repository or file not found"`, `422 → validation message from `detail``, `5xx / network → "API unavailable — is docker compose up?"`, `CORS failure → hint about base URL config`.

### Decision 9 — Security and credential boundaries

**Choice:**

- Browser never sees: `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `MODEL_API_KEY`, raw Cypher, or internal error stacks. API error responses are sanitized (Iter 8 contract) and the UI relays them without augmenting with internal details.
- `localStorage` holds only `activeRepositoryId` and UI preferences (theme, panel widths); no tokens or secrets. `sessionStorage` is not required.
- Content-Security-Policy is set loosely for the demo (`default-src 'self'` + `connect-src 'self' http://localhost:*` if serving cross-origin dev). No `eval` in UI code.

### Decision 10 — Testing and verification approach for the UI

**Choice:**

- Unit: `web` JS helpers (`lib/api.js` URL builder, error mapper; `evidence-panel` formatter) tested with Vitest or plain `node:test` mocks — no browser needed.
- Integration: FastAPI static-mount test — `TestClient(app).get("/")` returns HTML with `index.html` marker; `TestClient.get("/ui/app.js")` returns 200; `TestClient.post("/chat")` path remains reachable.
- E2E (smoke): Playwright or a minimal `httpx` + `curl` smoke script — start API (`uvicorn`) and serve `web/`, assert: repo list renders, selecting a repo enables chat/report/graph, chat round-trip returns mocked evidence, report shows breakdown, graph shows nodes. Iteration 11 ships the smoke harness; full E2E suite is completed in Iteration 12 with a demo repository fixture.

## Risks / Trade-offs

- **[Risk] CORS misconfiguration blocks dev-server demo** → Mitigation: ship `CORSMiddleware` with documented defaults; test both same-origin (`window.location.origin`) and cross-origin (`http://localhost:5173 → http://localhost:8000`) paths in smoke test.
- **[Risk] Graph visualization is unreadable on large neighborhoods (500+ edges)** → Mitigation: bound to top-N neighbors (default 30), show truncation note, provide list fallback (table of relationships) alongside the canvas.
- **[Risk] API base-URL drift between local, Docker, and docs** → Mitigation: runtime injection via `window.__CHRONOTRACE_CONFIG__` plus fallback chain; smoke test hits `curl -f http://localhost:${API_PORT}/health` and `curl -f http://localhost:${API_PORT}/` (or `/ui`) before demo.
- **[Risk] Chat streaming vs non-streaming API mismatch** → Mitigation: UI handles both — if response arrives as JSON, render atomically; if SSE `text/event-stream`, progressively append; never assume one transport.
- **[Risk] Score invention if UI duplicates logic** → Mitigation: frontend contains zero scoring/decay/ranking code; grep check (`rg "decay|WEIGHTS|confidence =" web/` fails in CI); report/graph values are copied verbatim from API JSON.
- **[Risk] Secret leakage into `web/dist` at build time** → Mitigation: build step never reads `.env`; `apiBaseUrl` is the only config injected at runtime into `window.__CHRONOTRACE_CONFIG__`; CI checks bundle for `NEO4J` / `MODEL_API_KEY` strings.
- **[Risk] Empty/failed ingestion state not communicated** → Mitigation: repository list shows `status` (pending/cloning/completed/failed) from API; un-ingested or failed repos show a badge and disable report/graph with explanatory CTA.
- **[Risk] FastAPI static mount conflicts with API routes** → Mitigation: mount static at `/ui` and keep `/`, `/health`, `/ready`, `/api/*`, `/repositories`, `/chat`, `/reports` reserved; root `/` may redirect or serve `index.html` via explicit `GET /` handler that falls through only when no API route matches, with `StaticFiles(html=True)`.
- **[Risk] Vanilla JS maintainability if UI grows** → Mitigation: component boundaries (`lib/`, `components/`) mirror the API modules; migrating to a framework later touches only `web/` without changing the API or analytics contracts.

## Migration Plan

1. Land `web/` static assets and `lib/api.js` + `lib/store.js` + component scaffolding; no backend change required to serve them via `python -m http.server` for early review.
2. Add `src/chronotrace/api/static.py` helper and `app.mount("/ui", ...)` (or equivalent) in `src/chronotrace/api/main.py`; add `CORSMiddleware` with config-driven `allow_origins`. Verify `TestClient` still passes existing API tests.
3. Add optional `GET /graph` alias if the team opts for a dedicated graph endpoint; otherwise confirm `POST /reports` already exposes the adjacency needed for the graph.
4. Update `Dockerfile` to `COPY web/dist ./web/dist` (or `COPY web ./web` for vanilla) and document that `docker compose up` now serves UI at `http://localhost:<api-port>/` or `/ui`. No Compose networking change; Neo4j still at `neo4j:7687` internally.
5. Run smoke: `curl -f http://localhost:8000/health && curl -f http://localhost:8000/ui/ && curl -f http://localhost:8000/api/repositories` (or equivalent) plus Playwright E2E with a disposable API mock.
6. Rollback: removing `web/` and the static mount restores pre-Iteration 11 behavior; API contracts unchanged, so CLI and MCP are unaffected. No graph migration to revert.

## Open Questions

- **Which graph library is preferred given bundle-size vs. interactivity trade-offs for the demo?** Deferred to implementation — any of `vis-network`, `cytoscape.js`, or D3-force satisfies the spec; final choice is not a spec change and can be swapped while preserving the evidence-panel contract.
- **Should chat support WebSocket for streaming, or is SSE/polling sufficient?** Current API (Iter 7/8) uses HTTP `POST /chat` — UI implements both non-streaming and SSE progressive rendering and will adopt WebSocket only if the API later exposes it, with no spec impact.
