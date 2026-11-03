# Config

Validated configuration for ChronoTrace. Implements the single-loader contract: only `src/chronotrace/config` may read `chronotrace.yml` or `os.environ`.

## Usage

```python
from chronotrace.config import load_config, validate_startup

config = load_config()  # finds chronotrace.yml, overlays env/.env
report = validate_startup(config, env=None, check_neo4j=False)
if not report["ok"]:
    raise SystemExit(report["errors"])
```

## Precedence

`CLI overrides > env/.env > chronotrace.yml > defaults`

Env keys: `CHRONOTRACE_*`, `NEO4J_*`, `MODEL_*` (see `.env.example`). Secrets (`MODEL_API_KEY`, `NEO4J_PASSWORD`) are required from env and never stored in YAML; Neo4j defaults to `neo4j/password` for internal Docker.

## Validation

- `version: 1` and `extra="forbid"` — unknown keys are rejected
- Paths (`project.data_dir`, `repositories_dir`) are `~`-expanded, created, and writability-probed
- Filters and bug-fix patterns are validated as glob/regex
- `graph.batch_size` 1..10000
