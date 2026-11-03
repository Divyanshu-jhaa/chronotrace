# Design: iter-0-foundation — Repository Foundation and Python Package

## Context

Current state (read-only inspection, 2026-11-03):
- `pyproject.toml` exists with `setuptools` build-system, `name="chronotrace" version="0.1.0"`, `requires-python >=3.12`, `where=["src"]`, console script `chronotrace = "chronotrace.cli.main:app"`, and deps for `typer/rich/httpx/pyyaml/pydriller/neo4j/fastapi/uvicorn/pydantic/python-dotenv` plus dev deps `pytest/pytest-cov/ruff/mypy`. `README.md` is absent. `Dockerfile` is empty. `docker-compose.yml` only defines `neo4j`.
- `src/chronotrace/__init__.py` is empty (0 bytes). `src/chronotrace/cli/main.py` defines `app = typer.Typer(name="chronotrace", help="AI-Powered Temporal Code Intelligence")` with `@app.callback()` and `@app.command() def version(): typer.echo("ChronoTrace 0.1.0")` — hard-coded version.
- `.gitignore` contains only `.env` and `myenv`. `.env.example` contains `NEO4J_USER/PASSWORD`, `MODEL_BASE_URL/API_KEY/NAME`, `MCP_HOST/PORT`, `API_HOST/PORT`.
- Attempts to respect `PLAN.md §4/§5/§6 Iteration 0` constraints: `src` layout, single-source version, Typer + Rich, installable editable, no API/Neo4j/Docker/analytics in this iteration.

See `proposal.md` for motivation; this doc covers how to complete the foundation without breaking the existing editable install (which currently works: `chronotrace --help` and `chronotrace version` both succeed).

## Goals / Non-Goals

**Goals:**
- Make `chronotrace.__version__` and `pyproject.toml version` a single source of truth, with `cli/main.py version` reading from `importlib.metadata.version()` at runtime and falling back to `__version__` for editable installs.
- Complete `.gitignore` and `.env.example` to satisfy Iteration 0 acceptance: no secrets tracked, documented placeholders, ignored venv/cache/artifacts.
- Add `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.format]`, `[tool.mypy]`, and `[tool.pytest.ini_options]` / `[tool.coverage.*]` config so `ruff check`, `ruff format --check`, `mypy src`, and `pytest -q` pass from repo root.
- Keep the change planning-only for this artifact; implementation details remain in `tasks.md`.

**Non-Goals:**
- No FastAPI, Neo4j driver, ingestion, static analysis, graph schema, analytics, MCP, agent, or Docker wiring (Iterations 1–9).
- No `chronotrace.yml` validation logic (Iteration 1).
- No CI pipeline, pre-commit hooks, or publishing to PyPI.
- No refactoring of `chronotrace.yml` / `docker-compose.yml` beyond what Iteration 0 requires; they remain as checked in.

## Decisions

### 1. Single-source version via `importlib.metadata` with `__version__` re-export

**Choice:** `src/chronotrace/__init__.py` defines `__version__ = "0.1.0"` and `pyproject.toml [project] version` stays `0.1.0`. `cli/main.py` does `from importlib.metadata import version, PackageNotFoundError` and resolves `try: __version__ = version("chronotrace") except PackageNotFoundError: from chronotrace import __version__`. The `version` command prints that value via `typer.echo`.

**Rationale:** Satisfies PLAN.md acceptance "version command returns the package version from one authoritative source" while keeping editable and source-checkout runs working when metadata is unavailable. Avoids hard-coding `"0.1.0"` in `main.py`.

**Alternatives considered:**
- `importlib.metadata` only → fails before `pip install -e .` (bad DX).
- `__version__` only with no metadata read → diverges from installed distribution version after `pip install`.
- `setuptools_scm` dynamic versioning → unnecessary complexity for 0.1.0 foundation; defer to Iteration 12 hardening if needed.

### 2. setuptools + src-layout (keep, do not switch to hatch/poetry/pdm)

**Choice:** Retain `setuptools>=68` + `wheel` and `[tool.setuptools.packages.find] where=["src"]`. No migration to Hatchling/Poetry.

**Rationale:** `pyproject.toml` and `src/chronotrace.egg-info` already validate with `setuptools`; switching build backends would churn Iteration 0 with no functional gain. `src` layout already satisfies test-import isolation per acceptance.

**Alternatives considered:** Hatchling (faster, modern) — rejected to avoid re-validating the existing editable install.

### 3. Typer CLI shape: `app` at `chronotrace.cli.main:app` with callback + version command

**Choice:** Keep `app = typer.Typer(name="chronotrace", help="AI-Powered Temporal Code Intelligence")`, `@app.callback()` for top-level help, `@app.command() def version(): ...` with docstring "Print the installed ChronoTrace version." Add `rich` for future output (not required for `version`).

**Rationale:** Matches PLAN.md §5 (Typer as CLI framework, Rich for terminal output) and the already-working entry point. No need for `click` or `argparse`.

### 4. ruff as sole linter/formatter; mypy for types

**Choice:** Configure in `pyproject.toml`:
- `[tool.ruff]` with `target-version = "py312"`, `line-length = 100`
- `[tool.ruff.lint]` with `select = ["E","F","I","UP","B"]` (pycodestyle, pyflakes, isort, pyupgrade, flake8-bugbear) and `ignore` minimal
- `[tool.ruff.format]` defaults
- `[tool.mypy]` with `python_version = "3.12"`, `strict = false` initially, `warn_return_any = true`, `ignore_missing_imports = true` for third-party stubs, `mypy_path = "src"`

**Rationale:** `ruff` replaces `flake8+isort+black` with one tool, compatible with `src` layout and fast CI. `mypy` covers type hygiene without blocking Iteration 0 with `strict = true` (escalate in Iteration 12).

**Alternatives considered:** `black+isort+flake8` — more config, slower; `pyright` — stricter but not in PLAN.md.

### 5. pytest + pytest-cov central config

**Choice:** Keep `[tool.pytest.ini_options] testpaths = ["tests"]` and add `addopts = "-q"` plus optional `[tool.coverage.run] source = ["src"]` and `[tool.coverage.report]`.

**Rationale:** Already present; minimal extension ensures `pytest -q` and `pytest --cov=chronotrace` work without `conftest.py` path hacks.

### 6. .gitignore completeness

**Choice:** Extend current 2-line file to cover: `.env`, `.venv/`, `venv/`, `env/`, `myenv/`, `__pycache__/`, `*.pyc`, `*.pyo`, `*.pyd`, `*.egg-info/`, `build/`, `dist/`, `.pytest_cache/`, `.coverage`, `htmlcov/`, `.mypy_cache/`, `.ruff_cache/`, `.DS_Store`, `Thumbs.db`, `.vscode/`, `.idea/`.

**Rationale:** Satisfies "No secrets or local environment files are tracked" and prevents accidental commit of venv/coverage/IDE artifacts. Scoped to Iteration 0; later iterations may add `neo4j_data/` etc. when volumes appear.

### 7. .env.example hygiene

**Choice:** Keep existing placeholders, ensure `MODEL_API_KEY=` is empty (no real key), `NEO4J_PASSWORD=change-this-locally` is an obvious placeholder, and add comments `# Copy to .env and fill in` header.

**Rationale:** Already satisfies spec; minor comment polish clarifies usage without introducing secrets. No YAML config leaked into `.env`.

## Risks / Trade-offs

- [Risk] Hard-coded `0.1.0` in `main.py` diverges from `pyproject.toml` → **Mitigation:** Replace with `importlib.metadata.version("chronotrace")` + fallback; add spec scenario checking no divergent literal remains.
- [Risk] `mypy` reports missing stubs for `pydriller`/`neo4j` → **Mitigation:** `ignore_missing_imports = true` and `[[tool.mypy.overrides]]` for known third-party modules; foundation code itself is typed.
- [Risk] `ruff` rules too strict break existing `main.py` → **Mitigation:** Start with `E/F/I/UP/B` and `line-length 100`; format once before validation.
- [Risk] `setuptools` `src` discovery picks up stale `src/chronotrace.egg-info` → **Mitigation:** Document `pip install -e .` regenerates metadata; `.gitignore` excludes `*.egg-info/`.
- [Risk] `.env` accidentally committed before ignore fix → **Mitigation:** `git check-ignore` scenario and pre-task `git ls-files` verification; no history rewrite in this iteration.
- [Risk] `requires-python >=3.12` breaks on older CI images → **Mitigation:** Acceptance explicitly targets 3.12; pip will refuse install with a clear message rather than silently running.

## Migration Plan

1. Edit `src/chronotrace/__init__.py` to set `__version__`.
2. Edit `src/chronotrace/cli/main.py` to read version from metadata.
3. Update `pyproject.toml` `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.format]`, `[tool.mypy]`, and extend `[tool.pytest.ini_options]` / add `[tool.coverage.*]`.
4. Replace `.gitignore` with complete ignore list.
5. Polish `.env.example` header comments (no secret values added).
6. Validate in a fresh venv: `pip install -e ".[dev]" && chronotrace --help && chronotrace version && pytest -q && ruff check . && ruff format --check . && mypy src`.
7. Rollback: `git checkout -- pyproject.toml src/chronotrace/__init__.py src/chronotrace/cli/main.py .gitignore .env.example` restores prior state; no DB or Docker state to revert.

## Open Questions

- None blocking Iteration 0. Deferred to later iterations:
  - Whether `Dockerfile` should be populated in Iteration 9 or remain empty until then (currently empty per inspection).
  - Whether to adopt `setuptools_scm` or `hatch-vcs` for versioning in Iteration 12 hardening.
