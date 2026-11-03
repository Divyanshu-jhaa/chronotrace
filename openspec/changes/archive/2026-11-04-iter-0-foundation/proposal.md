# Proposal: iter-0-foundation — Repository Foundation and Python Package

## Why

ChronoTrace is specified as a 13-iteration Python project (PLAN.md v1.0) but has no clean, installable foundation. Without a verified `src`-layout package, Typer entry point, single-source version, and baseline ignore/env/test/lint configuration, every later iteration risks path-dependent imports, untestable CLI wiring, and leaked secrets.

## What Changes

- Establish `src`-layout package `src/chronotrace/` with `src/chronotrace/__init__.py` as the single authoritative version source (matching `pyproject.toml`).
- Configure `pyproject.toml` with package metadata, `requires-python >=3.12`, runtime dependencies (`typer`, `rich`, `httpx`, `pyyaml`, `pydriller`, `neo4j`, `fastapi`, `uvicorn[standard]`, `pydantic`, `python-dotenv`), dev dependencies (`pytest`, `pytest-cov`, `ruff`, `mypy`), `[project.scripts]` console script `chronotrace = "chronotrace.cli.main:app"`, `[tool.setuptools.packages.find] where = ["src"]`, and `[tool.pytest.ini_options] testpaths = ["tests"]`.
- Create `src/chronotrace/cli/main.py` Typer application (`app = typer.Typer(name="chronotrace", help=...)`) with top-level `callback`, `version` command, and help text; wire `chronotrace` entry point so `chronotrace --help` lists commands and `chronotrace version` prints version without hard-coded duplication.
- Add/complete `.gitignore` to exclude `.env`, virtual environments (`.venv/`, `venv/`, `myenv/`, `env/`), Python cache/artifacts (`__pycache__/`, `*.pyc`, `*.pyo`, `*.egg-info/`, `build/`, `dist/`), coverage, and IDE/OS files.
- Add `.env.example` with documented placeholders for `MODEL_API_KEY`, `NEO4J_USERNAME`/`NEO4J_PASSWORD` (and related host/port/model vars) without real secrets; ensure `.env` itself is ignored.
- Add `chronotrace.yml.example` already present; keep as reference config for iteration 1.
- Configure `ruff` (lint + format) and `mypy` via `pyproject.toml` `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]` sections compatible with Python 3.12 and `src` layout.
- Ensure editable install (`pip install -e .` or `pip install -e ".[dev]"`) succeeds in a fresh virtual environment and that `tests` can `import chronotrace` without `sys.path` hacks.
- No API, Neo4j, Docker, ingestion, or analytics code in this iteration.

## Capabilities

### New Capabilities

- `foundation`: Repository foundation — installable `src`-layout Python package, Typer CLI entry point and `version` command, `.gitignore`/`.env.example` hygiene, pytest/ruff/mypy configuration, and editable-install verification.

### Modified Capabilities

- None — no prior specs exist (`openspec list --specs` returns empty).

## Impact

- **Code:** `pyproject.toml`, `src/chronotrace/__init__.py`, `src/chronotrace/cli/main.py`, `.gitignore`, `.env.example`, lint/type/test config in `pyproject.toml`.
- **APIs/Services:** None — no HTTP endpoints, no Neo4j, no MCP/Agent.
- **Dependencies:** Pins `setuptools>=68`, `wheel` build-system; runtime/dev deps listed above. No Docker images or Compose services added.
- **Tests/Tooling:** `pytest` discovers `tests/`; `ruff check`/`ruff format`/`mypy` runnable from repo root.
- **Breaking changes:** None — initial foundation; no downstream consumers to break.
- **Security:** `.env` ignored; `.env.example` contains no secrets.
