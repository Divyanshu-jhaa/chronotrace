# ChronoTrace

Evidence-first codebase risk analysis. Each sub-package owns one iteration and exposes a narrow interface; package boundaries follow `PLAN.md` and OpenSpec specs.

| Package | Iteration | Purpose |
|---|---|---|
| `config` | 1 | Validated `chronotrace.yml` + env loader, single-loader contract |
| `graph` | 2 | Neo4j driver, schema migrations, repository-isolated persistence |
| `ingestion` | 3 | Clone/fetch, PyDriller extraction, filtering, labeling, graph writes |
| `analytics` | 5 | Risk scoring and co-change analysis (planned) |
| `agent` | 7 | LLM agent orchestration (planned) |
| `api` | 8 | FastAPI service (planned) |
| `mcp` | 6 | MCP server (planned) |
| `cli` | 10 | Typer CLI (planned) |

All file paths are POSIX-normalized; all Cypher is parameterized; secrets never appear in YAML.
