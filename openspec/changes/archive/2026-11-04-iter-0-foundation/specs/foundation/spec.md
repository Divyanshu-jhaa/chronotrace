# Spec Delta — foundation

## Purpose

Provides the installable Python package foundation so all later ChronoTrace iterations have a verified src-layout, Typer CLI entry point, single-source version, and clean test/lint/env hygiene.

## ADDED Requirements

### Requirement: src-layout installable package

The repository SHALL provide an installable Python distribution using a `src` layout with `pyproject.toml` as the build descriptor and `src/chronotrace` as the import root.

#### Scenario: Editable install in fresh virtual environment

- **WHEN** a developer creates a fresh virtual environment with Python >=3.12 and runs `pip install -e .` (or `pip install -e ".[dev]"`) from the repository root
- **THEN** the install succeeds without `sys.path` manipulation and `import chronotrace` resolves to `src/chronotrace/__init__.py`

#### Scenario: Package metadata is authoritative

- **WHEN** `import importlib.metadata; importlib.metadata.version("chronotrace")` is called after installation
- **THEN** the returned version equals the `version` declared in `pyproject.toml` and equals `chronotrace.__version__` when defined

#### Scenario: Package discovery is scoped to src

- **WHEN** `pyproject.toml` `[tool.setuptools.packages.find]` is inspected
- **THEN** it declares `where = ["src"]` so only `src/chronotrace` is packaged and top-level shadowing is prevented

### Requirement: Package metadata and dependencies

The `pyproject.toml` SHALL declare project name `chronotrace`, description, `requires-python >=3.12`, build-system `setuptools>=68` with `wheel`, runtime dependencies, dev dependencies, and console script.

#### Scenario: Required runtime dependencies declared

- **WHEN** `pyproject.toml` `[project] dependencies` is inspected
- **THEN** it contains at least `typer`, `rich`, `httpx`, `pyyaml`, `pydriller`, `neo4j`, `fastapi`, `uvicorn[standard]`, `pydantic`, `python-dotenv`

#### Scenario: Dev dependencies declared

- **WHEN** `pyproject.toml` `[project.optional-dependencies] dev` is inspected
- **THEN** it contains `pytest`, `pytest-cov`, `ruff`, `mypy`

#### Scenario: Requires-Python enforces 3.12+

- **WHEN** `pyproject.toml` `requires-python` is read
- **THEN** it specifies `>=3.12` and installation under Python 3.11 or earlier is rejected by the installer

### Requirement: Typer CLI entry point and help

The package SHALL expose a `chronotrace` console script via `[project.scripts]` pointing to `chronotrace.cli.main:app` and provide a Typer application with discoverable help.

#### Scenario: Console script is available after install

- **WHEN** the package is installed and `chronotrace --help` is executed in a shell
- **THEN** the command exits 0 and lists the `version` command and `--help` options under a Typer-formatted help screen

#### Scenario: Top-level CLI does not require subcommand for help

- **WHEN** `chronotrace --help` is invoked with no subcommand
- **THEN** the output includes `AI-Powered Temporal Code Intelligence` (or the configured app help) and the `version` command entry

### Requirement: Version command with single source of truth

The CLI SHALL provide a `version` command that prints the installed package version obtained from a single authoritative source, without duplicating the version string.

#### Scenario: Version command prints installed version

- **WHEN** `chronotrace version` is executed after an editable install
- **THEN** stdout contains the package version (e.g., `0.1.0`) matching `importlib.metadata.version("chronotrace")`

#### Scenario: Version is not hard-coded in multiple places

- **WHEN** the codebase is inspected for version literals
- **THEN** the version string literal appears in exactly one authoritative location (`pyproject.toml` and a runtime read from `importlib.metadata` or a single `__version__` synchronized at build time), and `cli/main.py` does not contain a divergent hard-coded version

#### Scenario: Version works independent of working directory

- **WHEN** `chronotrace version` is run from a directory other than the repository root (e.g., `/tmp`)
- **THEN** it still prints the installed version without relying on `cwd` or presence of `pyproject.toml` on disk

### Requirement: .gitignore excludes secrets and artifacts

The repository SHALL provide a `.gitignore` that prevents secret, environment, and build-artifact files from being tracked.

#### Scenario: .env is ignored

- **WHEN** `git check-ignore -q .env` is run (or `.gitignore` is inspected)
- **THEN** `.env` matches an ignore pattern and `git status` does not show `.env` as untracked when present

#### Scenario: Virtual environments and caches are ignored

- **WHEN** `.gitignore` is inspected
- **THEN** it contains entries covering `.venv/`, `venv/`, `env/`, `myenv/`, `__pycache__/`, `*.pyc`, `*.pyo`, `*.egg-info/`, `build/`, `dist/`, `.pytest_cache/`, `.coverage`, `htmlcov/`, `.mypy_cache/`, `.ruff_cache/`

#### Scenario: No secrets file is tracked

- **WHEN** `git ls-files` is enumerated
- **THEN** `.env` and any file matching `*.key`, `*.pem`, or a committed `.env` variant does not appear in the index

### Requirement: .env.example provides safe placeholders

The repository SHALL provide `.env.example` with documented placeholder entries for required runtime secrets and service configuration, containing no real secret values.

#### Scenario: .env.example documents required variables

- **WHEN** `.env.example` is read
- **THEN** it contains placeholders for `MODEL_API_KEY`, `NEO4J_USERNAME`/`NEO4J_USER`, `NEO4J_PASSWORD`, and at least `MCP_HOST`/`MCP_PORT` and `API_HOST`/`API_PORT` or model base URL/name entries with empty or `change-me` values

#### Scenario: .env.example contains no real secrets

- **WHEN** `.env.example` is scanned
- **THEN** no value resembles a committed real API key or password (e.g., no non-placeholder `sk-` key, no literal `neo4j/password` used as a committed credential beyond an obvious placeholder), and the file is safe to commit

### Requirement: pytest configuration

The repository SHALL configure `pytest` via `pyproject.toml` `[tool.pytest.ini_options]` so tests are discoverable without working-directory hacks.

#### Scenario: pytest discovers tests directory

- **WHEN** `pytest --collect-only -q` is run from the repository root
- **THEN** collection uses `testpaths = ["tests"]` from `pyproject.toml` and `tests/` imports `chronotrace` without `PYTHONPATH` or `sys.path` insertion

#### Scenario: Tests can import package

- **WHEN** a file `tests/test_version.py` containing `import chronotrace; import chronotrace.cli.main` is executed via `pytest`
- **THEN** the import succeeds under the installed package and does not depend on the current working directory

### Requirement: Lint and type-check configuration

The repository SHALL configure `ruff` (lint + format) and `mypy` via `pyproject.toml` with settings compatible with Python 3.12 and the `src` layout.

#### Scenario: ruff check passes on foundation code

- **WHEN** `ruff check .` is run from the repository root
- **THEN** the foundation files (`src/chronotrace/__init__.py`, `src/chronotrace/cli/main.py`) pass without errors under the configured `[tool.ruff]` and `[tool.ruff.lint]` rules

#### Scenario: ruff format is configured

- **WHEN** `pyproject.toml` `[tool.ruff]` or `[tool.ruff.format]` is inspected
- **THEN** a format configuration exists and `ruff format --check .` exits 0 on already-formatted foundation files

#### Scenario: mypy is configured for src layout

- **WHEN** `pyproject.toml` `[tool.mypy]` is inspected and `mypy src` is run
- **THEN** mypy resolves `chronotrace` from `src/` (via `mypy_path` or `packages` discovery) and does not report missing-import errors for the installed package itself

### Requirement: Editable install verification and test suite smoke

The foundation SHALL be verifiable by an automated smoke check that the package installs, the CLI runs, and a minimal test suite passes.

#### Scenario: Editable install smoke

- **WHEN** `pip install -e ".[dev]"` is run in a clean venv and then `chronotrace version` and `chronotrace --help` are executed
- **THEN** both commands exit 0, `chronotrace version` prints the version, and `chronotrace --help` lists the version command

#### Scenario: Initial test suite passes

- **WHEN** `pytest -q` is run from the repository root after editable install
- **THEN** all foundation tests (e.g., version/help/import smoke tests) pass and `pytest-cov` can generate a coverage report without configuration errors
