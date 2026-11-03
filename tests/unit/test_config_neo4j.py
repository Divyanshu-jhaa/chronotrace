"""Test Neo4j connectivity distinction."""

from unittest.mock import MagicMock, patch

import pytest

from chronotrace.config import load_config, validate_startup
from chronotrace.config.exceptions import ConnectivityError


def test_neo4j_success(monkeypatch, tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(
        config_path=p, env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    )
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value = mock_session
    mock_session.run.return_value.consume.return_value = None
    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        validate_startup(
            cfg,
            env={
                "MODEL_API_KEY": "k",
                "NEO4J_USERNAME": "u",
                "NEO4J_PASSWORD": "p",
                "NEO4J_URI": "bolt://localhost:7687",
            },
            check_neo4j=True,
        )
        assert True


def test_neo4j_auth_error(monkeypatch, tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(
        config_path=p, env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    )
    mock_driver = MagicMock()
    mock_driver.verify_connectivity.side_effect = Exception("authentication failed")
    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        with pytest.raises(ConnectivityError, match="configuration error"):
            validate_startup(
                cfg,
                env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"},
                check_neo4j=True,
            )


def test_neo4j_unavailable(monkeypatch, tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text("version: 1\n")
    cfg = load_config(
        config_path=p, env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"}
    )
    mock_driver = MagicMock()
    mock_driver.verify_connectivity.side_effect = Exception("connection refused")
    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        with pytest.raises(ConnectivityError, match="unavailable"):
            validate_startup(
                cfg,
                env={"MODEL_API_KEY": "k", "NEO4J_USERNAME": "u", "NEO4J_PASSWORD": "p"},
                check_neo4j=True,
            )
