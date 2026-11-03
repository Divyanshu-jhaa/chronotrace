"""Test validate_startup."""

from chronotrace.config import load_config, validate_startup
from chronotrace.config.exceptions import FilesystemError


def test_missing_model_key(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(config_path=p, env={})
    report = validate_startup(cfg, env={}, check_neo4j=False)
    assert not report["ok"]
    assert any("MODEL_API_KEY" in e for e in report["errors"])


def test_missing_neo4j(tmp_path):
    # Neo4j is now zero-config (defaults to docker-compose neo4j/password),
    # so missing NEO4J env is OK when only MODEL_API_KEY is present.
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {"MODEL_API_KEY": "key", "NEO4J_USERNAME": "", "NEO4J_PASSWORD": ""}
    cfg = load_config(config_path=p, env=env)
    report = validate_startup(cfg, env=env, check_neo4j=False)
    assert report["ok"]
    assert not any("NEO4J" in e for e in report["errors"])


def test_filesystem_ok(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    env = {"MODEL_API_KEY": "key", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    cfg = load_config(config_path=p, env=env)
    report = validate_startup(cfg, env=env, check_neo4j=False)
    # Should be ok if dirs are writable (tmp)
    assert report["ok"] is True or any("not writable" not in e for e in report["errors"])


def test_no_db_connection_without_check(tmp_path, monkeypatch):
    # Ensure validate_startup without check_neo4j does not try to connect
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(
        config_path=p, env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    )
    # Mock neo4j to ensure not called
    monkeypatch.setattr(
        "neo4j.GraphDatabase.driver",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not connect")),
    )
    # Should not raise because check_neo4j=False
    report = validate_startup(
        cfg,
        env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"},
        check_neo4j=False,
    )
    assert report["ok"]


def test_non_writable_dir(tmp_path, monkeypatch):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(
        config_path=p, env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    )
    # Mock _expand_and_ensure_dir to raise
    monkeypatch.setattr(
        "chronotrace.config.loader._expand_and_ensure_dir",
        lambda raw, field: (_ for _ in ()).throw(
            FilesystemError(f"Directory for {field} is not writable: {raw}")
        ),
    )
    report = validate_startup(
        cfg, env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    )
    assert not report["ok"]
