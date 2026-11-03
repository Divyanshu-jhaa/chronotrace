# Tasks: iter-0-foundation — Repository Foundation and Python Package

## 1. Package metadata and src layout

- [x] 1.1 Verify `src` layout and `pyproject.toml` metadata — confirm `src/chronotrace/__init__.py` exists, `pyproject.toml` declares `name="chronotrace"`, `version="0.1.0"`, `requires-python >=3.12`, `setuptools>=68` build-system, and `where=["src"]`; verify `python -c "import importlib.metadata; print(importlib.metadata.version('chronotrace'))"` matches `pyproject.toml` after editable install
- [x] 1.2 Declare runtime and dev dependencies — ensure `[project] dependencies` lists `typer`, `rich`, `httpx`, `pyyaml`, `pydriller`, `neo4j`, `fastapi`, `uvicorn[standard]`, `pydantic`, `python-dotenv` and `[project.optional-dependencies] dev` lists `pytest`, `pytest-cov`, `ruff`, `mypy`; verify `pip install -e ".[dev]" --dry-run` or `pip check` shows no missing deps
- [x] 1.3 Expose `chronotrace` console script — ensure `[project.scripts] chronotrace = "chronotrace.cli.main:app"` is present and `pip show -f chronotrace` lists the entry point; verify `which chronotrace` / `where chronotrace` resolves after install

## 2. Single-source version and Typer CLI

- [x] 2.1 Author `src/chronotrace/__init__.py` single version source — set `__version__ = "0.1.0"` (or import from `importlib.metadata` with fallback) so `from chronotrace import __version__` and `importlib.metadata.version("chronotrace")` agree; verify `python -c "import chronotrace; print(chronotrace.__version__)"` prints `0.1.0`
- [x] 2.2 Wire `src/chronotrace/cli/main.py` Typer app — keep `app = typer.Typer(name="chronotrace", help="AI-Powered Temporal Code Intelligence")`, `@app.callback()`, and `@app.command() def version()` that resolves version via `importlib.metadata.version("chronotrace")` with fallback and prints via `typer.echo`; verify `python -m chronotrace.cli.main --help` exits 0
- [x] 2.3 Verify CLI entry point behavior — run `chronotrace --help` lists `version` command and help text, `chronotrace version` prints `0.1.0` (or current version), and `chronotrace version` from `/tmp` (non-repo cwd) still succeeds; verify no hard-coded version divergence via `rg -n "0\.1\.0" src/chronotrace/cli/main.py` shows zero hits after fix

## 3. Hygiene: .gitignore and .env.example

- [x] 3.1 Complete `.gitignore` — replace 2-line file with entries for `.env`, `.venv/`, `venv/`, `env/`, `myenv/`, `__pycache__/`, `*.pyc`, `*.pyo`, `*.egg-info/`, `build/`, `dist/`, `.pytest_cache/`, `.coverage`, `htmlcov/`, `.mypy_cache/`, `.ruff_cache/`, `.DS_Store`, `Thumbs.db`, `.vscode/`, `.idea/`; verify `git check-ignore -q .env && git check-ignore -q myenv && git check-ignore -q src/__pycache__` (or equivalent path) all match
- [x] 3.2 Harden `.env.example` placeholders — ensure file contains `MODEL_API_KEY=`, `NEO4J_USER`/`NEO4J_PASSWORD=change-this-locally`, `MODEL_BASE_URL`, `MODEL_NAME`, `MCP_HOST/PORT`, `API_HOST/PORT` with no real secrets and a header comment `# Copy to .env and fill in`; verify `git ls-files | rg -q "^\.env$"` is false (only `.env.example` tracked) and scanning `.env.example` shows no `sk-` real key

## 4. Test, lint, and type configuration

- [x] 4.1 Configure pytest and coverage — in `pyproject.toml` keep `[tool.pytest.ini_options] testpaths = ["tests"]`, add `addopts`/markers as needed and optional `[tool.coverage.run] source=["src"]`; verify `pytest --collect-only -q` discovers `tests/` and `pytest -q` passes on a minimal `tests/test_version.py` that imports `chronotrace` and invokes `Typer` test runner
- [x] 4.2 Configure ruff lint and format — add `[tool.ruff]` (`target-version="py312"`, `line-length=100`), `[tool.ruff.lint] select=["E","F","I","UP","B"]`, `[tool.ruff.format]`; verify `ruff check .` exits 0 and `ruff format --check .` exits 0 on `src/chronotrace/__init__.py` and `src/chronotrace/cli/main.py`
- [x] 4.3 Configure mypy for src layout — add `[tool.mypy]` (`python_version="3.12"`, `mypy_path="src"`, `ignore_missing_imports=true`, `warn_return_any=true`) and verify `mypy src` reports no errors on foundation modules (or only ignored missing stubs for third-party)

## 5. Editable-install and acceptance verification

- [x] 5.1 Fresh-venv editable install smoke — in a clean venv run `pip install -e ".[dev]"` then `chronotrace --help` (lists `version`), `chronotrace version` (prints version), and `python -c "import chronotrace; import chronotrace.cli.main; print('ok')"`; verify all exit 0
- [x] 5.2 Foundation test suite green — run `pytest -q` and `pytest --cov=chronotrace -q` from repo root; verify tests pass, package imports without `PYTHONPATH`/`sys.path` hacks, and coverage report generates without config errors
- [x] 5.3 Lint/type gate green — run `ruff check . && ruff format --check . && mypy src` from repo root; verify zero errors on foundation files, confirming `pyproject.toml` tooling is compatible with Python 3.12 and `src` layout
- [x] 5.4 Secret-hygiene gate — run `git ls-files | rg -v "^\.env\.example"` to confirm `.env` not in index, `git check-ignore -q .env` succeeds, and `.env.example` contains no real secret literals; verify acceptance criteria "No secrets or local environment files are tracked" holds

