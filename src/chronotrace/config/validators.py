"""Reusable validators for config fields."""

from __future__ import annotations

import re

from .exceptions import ConfigValidationError

_ALLOWED_LANGUAGES = {"python"}
_ALLOWED_GRANULARITY = {"file"}
_ALLOWED_GRAPH_PROVIDERS = {"neo4j"}
_ALLOWED_AGENT_PROVIDERS = {"meta"}
_ALLOWED_REASONING = {"low", "medium", "high"}


def validate_positive_int(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"{field} must be a positive integer, got {value!r}")
    if value <= 0:
        raise ConfigValidationError(f"{field} must be > 0, got {value}")
    return value


def validate_positive_float(value: float | int, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigValidationError(f"{field} must be a positive number, got {value!r}")
    fv = float(value)
    if fv <= 0:
        raise ConfigValidationError(f"{field} must be > 0, got {value}")
    return fv


def validate_null_or_positive_int(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    return validate_positive_int(value, field)


def validate_regex(pattern: str, field: str) -> str:
    try:
        re.compile(pattern)
    except re.error as e:
        raise ConfigValidationError(f"{field} contains invalid regex {pattern!r}: {e}") from e
    return pattern


def validate_regex_list(patterns: list[str], field: str) -> list[str]:
    for p in patterns:
        validate_regex(p, field)
    return patterns


def validate_language(value: str) -> str:
    if value not in _ALLOWED_LANGUAGES:
        raise ConfigValidationError(
            f"analysis.language must be one of {sorted(_ALLOWED_LANGUAGES)}, got {value!r}"
        )
    return value


def validate_granularity(value: str) -> str:
    if value not in _ALLOWED_GRANULARITY:
        raise ConfigValidationError(
            f"analysis.granularity must be one of {sorted(_ALLOWED_GRANULARITY)}, got {value!r}"
        )
    return value


def validate_graph_provider(value: str) -> str:
    if value not in _ALLOWED_GRAPH_PROVIDERS:
        raise ConfigValidationError(
            f"graph.provider must be one of {sorted(_ALLOWED_GRAPH_PROVIDERS)}, got {value!r}"
        )
    return value


def validate_agent_provider(value: str) -> str:
    if value not in _ALLOWED_AGENT_PROVIDERS:
        raise ConfigValidationError(
            f"agent.provider must be one of {sorted(_ALLOWED_AGENT_PROVIDERS)}, got {value!r}"
        )
    return value


def validate_reasoning_effort(value: str) -> str:
    if value not in _ALLOWED_REASONING:
        raise ConfigValidationError(
            f"agent.reasoning_effort must be one of {sorted(_ALLOWED_REASONING)}, got {value!r}"
        )
    return value
