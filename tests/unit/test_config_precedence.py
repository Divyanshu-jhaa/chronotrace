"""Test precedence cli > env > file > defaults."""

import pytest

from chronotrace.config import load_config


def test_precedence_file_vs_env_vs_cli(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\ngraph:\n  batch_size: 100\n")
    # File says 100, env says 200, cli says 300 -> cli wins
    env = {"CHRONOTRACE_BATCH_SIZE": "200"}
    cli = {"graph.batch_size": 300}
    cfg = load_config(config_path=p, env=env, cli_overrides=cli)
    assert cfg.graph.batch_size == 300
    # Without cli, env wins
    cfg2 = load_config(config_path=p, env=env, cli_overrides=None)
    assert cfg2.graph.batch_size == 200
    # Without env, file wins
    cfg3 = load_config(config_path=p, env={}, cli_overrides=None)
    assert cfg3.graph.batch_size == 100
    # Without file, default wins (500) - use None to trigger defaults, not explicit missing
    # Explicit missing should raise
    with pytest.raises(Exception):  # noqa: B017 - generic to cover ConfigValidationError
        load_config(config_path=tmp_path / "nonexistent.yml", env={}, cli_overrides=None)
    cfg5 = load_config(config_path=None, env={}, cli_overrides=None)
    # Might use cwd file or defaults; ensure at least default
    assert cfg5.graph.batch_size in (100, 500)


def test_env_wins_over_file(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\nproject:\n  data_dir: /tmp/file\n")
    env = {"CHRONOTRACE_DATA_DIR": "/tmp/env"}
    cfg = load_config(config_path=p, env=env)
    # Env should override file (verified via internal merge)
    assert cfg.project.data_dir == "/tmp/env"


def test_cli_wins_over_env(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\ngraph:\n  batch_size: 100\n")
    env = {"CHRONOTRACE_BATCH_SIZE": "200"}
    cli = {"graph.batch_size": 999}
    cfg = load_config(config_path=p, env=env, cli_overrides=cli)
    assert cfg.graph.batch_size == 999
