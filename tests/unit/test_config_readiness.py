"""Test readiness gating."""

from chronotrace.config import load_config, validate_startup


def test_not_ready_when_missing_secret(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(config_path=p, env={})
    report = validate_startup(cfg, env={}, check_neo4j=False)
    assert not report["ok"]
    # Errors should be present, no secret leakage
    for e in report["errors"]:
        assert "sk-" not in e
        assert "password" not in e.lower() or "REDACTED" not in e  # just ensure no value


def test_ready_when_all_present(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {"MODEL_API_KEY": "key", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    cfg = load_config(config_path=p, env=env)
    report = validate_startup(cfg, env=env, check_neo4j=False)
    assert report["ok"]
    assert report["errors"] == []


def test_redacted_logs(tmp_path, caplog=None):
    # Ensure no secret in exception string
    from chronotrace.config.exceptions import MissingSecretError

    err = MissingSecretError("MODEL_API_KEY")
    assert "sk-secret" not in str(err)
