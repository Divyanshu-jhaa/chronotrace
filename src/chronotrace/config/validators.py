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


# --- Agent extended validators for iter-7 ---

# Verified model identifier per https://dev.meta.ai/docs/models and chronotrace.yml
# default muse-spark-1.3. Contributor variant is also advertised.
_ALLOWED_AGENT_MODELS = {"muse-spark-1.3", "muse-spark-1.3-contributor"}
_AGENT_MODEL_RE = re.compile(r"^muse-spark-1\.3(-contributor)?$")

# Verified Meta Model API endpoint
_VERIFIED_AGENT_BASE_URL = "https://api.meta.ai/v1"


def validate_agent_model(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"agent.model must be a non-empty string, got {value!r}")
    v = value.strip()
    if v not in _ALLOWED_AGENT_MODELS and not _AGENT_MODEL_RE.match(v):
        allowed = sorted(_ALLOWED_AGENT_MODELS)
        pat = _AGENT_MODEL_RE.pattern
        raise ConfigValidationError(
            f"agent.model must be one of {allowed} (pattern {pat!r}), got {v!r}"
        )
    return v


def validate_agent_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"agent.base_url must be a non-empty HTTPS URL, got {value!r}")
    v = value.strip()
    # Must be HTTPS
    if not v.startswith("https://"):
        msg = f"agent.base_url must be HTTPS URL starting with https://, got {v!r}"
        raise ConfigValidationError(msg)
    # Basic URL structure check
    try:
        from urllib.parse import urlparse

        parsed = urlparse(v)
        if not parsed.netloc:
            raise ConfigValidationError(f"agent.base_url must be a valid HTTPS URL, got {v!r}")
        if parsed.scheme != "https":
            raise ConfigValidationError(f"agent.base_url must use https scheme, got {v!r}")
    except ConfigValidationError:
        raise
    except Exception as e:
        raise ConfigValidationError(
            f"agent.base_url must be valid HTTPS URL, got {v!r}: {e}"
        ) from e
    return v


def validate_agent_max_iterations(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"agent.max_iterations must be an integer, got {value!r}")
    if value < 1 or value > 32:
        raise ConfigValidationError(f"agent.max_iterations must be 1..32, got {value!r}")
    return value


def validate_agent_timeout_seconds(value: float | int) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigValidationError(f"agent.timeout_seconds must be a number, got {value!r}")
    fv = float(value)
    if fv < 1:
        raise ConfigValidationError(f"agent.timeout_seconds must be >= 1, got {value!r}")
    return fv


def validate_agent_history_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"agent.history_limit must be an integer, got {value!r}")
    if value < 1:
        raise ConfigValidationError(f"agent.history_limit must be >= 1, got {value!r}")
    return value
