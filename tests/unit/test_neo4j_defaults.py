"""Neo4j zero-config defaults — docker-compose neo4j/password."""

from unittest.mock import MagicMock, patch

from chronotrace.config import load_config, validate_startup
from chronotrace.graph.driver import GraphDriver


def test_validate_startup_only_model_key_required(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(config_path=p, env={"MODEL_API_KEY": "k"})
    report = validate_startup(cfg, env={"MODEL_API_KEY": "k"}, check_neo4j=False)
    assert report["ok"]
    assert report["errors"] == []


def test_graph_driver_defaults_to_compose(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(config_path=p, env={"MODEL_API_KEY": "k"})
    # Patch neo4j GraphDatabase to capture auth
    with patch("neo4j.GraphDatabase") as MockDB:
        mock_inst = MagicMock()
        MockDB.driver.return_value = mock_inst
        driver = GraphDriver(cfg, env={})
        # Should default to neo4j/password from docker-compose.yml
        args, kwargs = MockDB.driver.call_args
        assert kwargs.get("auth") == ("neo4j", "password")
        assert driver._username == "neo4j"
        assert driver._password == "password"
        driver.close()
