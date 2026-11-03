"""API guard — no agent without validated key."""

import pytest

from chronotrace.api.dependencies import get_validated_config
from chronotrace.config.exceptions import ChronoTraceConfigError


def test_agent_blocked_without_key(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {}
    with pytest.raises(ChronoTraceConfigError):
        get_validated_config(config_path=str(p), env=env, check_neo4j=False)


def test_agent_allowed_with_key(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {"MODEL_API_KEY": "valid-key", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    config, report = get_validated_config(config_path=str(p), env=env, check_neo4j=False)
    assert report["ok"]
    assert config.version == 1


def test_api_guard_uses_validate_startup(tmp_path, monkeypatch):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    # Mock validate_startup to return not ok
    monkeypatch.setattr(
        "chronotrace.api.dependencies.validate_startup",
        lambda *a, **k: {"ok": False, "errors": ["forced not ready"], "warnings": []},
    )
    with pytest.raises(ChronoTraceConfigError, match="forced not ready"):
        get_validated_config(config_path=str(p), env=env)
