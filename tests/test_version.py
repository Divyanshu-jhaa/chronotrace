import importlib.metadata

from typer.testing import CliRunner

import chronotrace
from chronotrace.cli.main import app


def test_import_chronotrace():
    assert chronotrace is not None
    assert hasattr(chronotrace, "__version__")
    assert chronotrace.__version__ == "0.1.0"


def test_version_matches_metadata():
    assert chronotrace.__version__ == importlib.metadata.version("chronotrace")


def test_cli_import():
    from chronotrace.cli.main import app as cli_app

    assert cli_app is not None


def test_cli_version_command():
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout
    assert result.stdout.strip() == "0.1.0"


def test_cli_help_lists_version():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.stdout.lower()
    assert "AI-Powered Temporal Code Intelligence" in result.stdout
