"""Test loader file handling."""

import pathlib

import pytest

from chronotrace.config.exceptions import ConfigValidationError
from chronotrace.config.loader import find_config_file, load_config


def test_missing_file():
    with pytest.raises(ConfigValidationError, match="not found"):
        load_config(config_path=pathlib.Path("/tmp/does_not_exist_chronotrace_xyz.yml"))


def test_malformed_yaml(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("version: [unclosed\n")
    with pytest.raises(ConfigValidationError, match="Malformed YAML"):
        load_config(config_path=p)


def test_valid_file(tmp_path):
    p = tmp_path / "good.yml"
    p.write_text("version: 1\nproject:\n  name: test\n")
    cfg = load_config(config_path=p, env={})
    assert cfg.version == 1
    assert cfg.project.name == "test"


def test_find_config_file_explicit(tmp_path):
    p = tmp_path / "custom.yml"
    p.write_text("version: 1\n")
    found = find_config_file(explicit=p)
    assert found == p.resolve()


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("version: 1\nunknown_top: true\n")
    with pytest.raises(ConfigValidationError):
        load_config(config_path=p, env={})
