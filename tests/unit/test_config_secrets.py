"""Test secret overlay."""

import pytest

from chronotrace.config import load_config
from chronotrace.config.exceptions import ConfigValidationError


def test_secrets_from_env(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {
        "MODEL_API_KEY": "test-key-123",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "pass123",
    }
    cfg = load_config(config_path=p, env=env)
    # Config loads without error, secrets are in env not config
    assert cfg.version == 1
    # Ensure secrets not in config object
    assert not hasattr(cfg, "MODEL_API_KEY")


def test_secrets_in_yaml_rejected(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\nMODEL_API_KEY: should-fail\n")
    with pytest.raises(ConfigValidationError, match="Extra inputs are not permitted|unknown"):
        load_config(config_path=p, env={})


def test_missing_secret_not_in_error(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {"MODEL_API_KEY": "sk-1234567890abcdef"}
    load_config(config_path=p, env=env)
    from chronotrace.config.exceptions import ConfigValidationError, MissingSecretError

    err = MissingSecretError("MODEL_API_KEY")
    assert "sk-1234567890" not in str(err)
    err2 = ConfigValidationError("bad value sk-1234567890 should be hidden")
    assert "sk-1234567890" not in str(err2)


def test_optional_model_env(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {"MODEL_BASE_URL": "https://example.com/v1", "MODEL_NAME": "custom-model"}
    cfg = load_config(config_path=p, env=env)
    assert cfg.version == 1
