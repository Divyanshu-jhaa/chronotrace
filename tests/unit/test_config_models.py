"""Test Pydantic models for config."""

import pathlib

import pytest
import yaml
from pydantic import ValidationError

from chronotrace.config.models import ChronoTraceConfig


def test_example_yaml_loads():
    p = pathlib.Path("chronotrace.yml")
    assert p.exists()
    data = yaml.safe_load(p.read_text())
    cfg = ChronoTraceConfig.model_validate(data)
    assert cfg.version == 1
    assert cfg.analysis.language == "python"
    assert cfg.analysis.granularity == "file"
    assert cfg.graph.provider == "neo4j"
    assert cfg.agent.provider == "meta"


def test_unsupported_version_rejected():
    with pytest.raises(Exception, match="version"):
        ChronoTraceConfig.model_validate({"version": 2})


def test_unknown_top_level_key_rejected():
    with pytest.raises(Exception, match="unknown"):
        ChronoTraceConfig.model_validate({"version": 1, "unknown_field": True})


def test_unknown_nested_key_rejected():
    with pytest.raises(ValidationError):
        ChronoTraceConfig.model_validate({"version": 1, "analysis": {"unknown_nested": 1}})


def test_invalid_batch_size():
    with pytest.raises(Exception, match="batch_size"):
        ChronoTraceConfig.model_validate({"version": 1, "graph": {"batch_size": 0}})


def test_invalid_decay():
    with pytest.raises(Exception, match="decay"):
        ChronoTraceConfig.model_validate(
            {"version": 1, "analysis": {"history": {"decay_half_life_days": 0}}}
        )


def test_invalid_max_commits():
    with pytest.raises(ValidationError):
        ChronoTraceConfig.model_validate(
            {"version": 1, "analysis": {"history": {"max_commits": 0}}}
        )
    # null and positive should pass
    cfg = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"max_commits": None}}}
    )
    assert cfg.analysis.history.max_commits is None
    cfg = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"max_commits": 5}}}
    )
    assert cfg.analysis.history.max_commits == 5


def test_invalid_regex():
    with pytest.raises(Exception, match="invalid"):
        ChronoTraceConfig.model_validate(
            {"version": 1, "analysis": {"filtering": {"exclude": ["[unclosed"]}}}
        )
    with pytest.raises(Exception, match="invalid"):
        ChronoTraceConfig.model_validate(
            {"version": 1, "analysis": {"bug_fix_labels": {"patterns": ["[bad"]}}}
        )


def test_valid_regex_and_glob():
    cfg = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"filtering": {"exclude": ["**/.git/**", r"\bfix\b"]}}}
    )
    assert "**/.git/**" in cfg.analysis.filtering.exclude


def test_invalid_language():
    with pytest.raises(Exception, match="language"):
        ChronoTraceConfig.model_validate({"version": 1, "analysis": {"language": "java"}})


def test_invalid_reasoning():
    with pytest.raises(Exception, match="reasoning"):
        ChronoTraceConfig.model_validate({"version": 1, "agent": {"reasoning_effort": "ultra"}})


def test_frozen():
    cfg = ChronoTraceConfig.model_validate({"version": 1})
    with pytest.raises(ValidationError):
        cfg.version = 2  # frozen
