"""Pydantic v2 models for chronotrace.yml schema v1."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .validators import (
    validate_agent_provider,
    validate_granularity,
    validate_graph_provider,
    validate_language,
    validate_reasoning_effort,
)


def _validate_regex_field(v: str) -> str:
    try:
        re.compile(v)
    except re.error as e:
        raise ValueError(f"invalid regex {v!r}: {e}") from e
    return v


def _validate_filter_pattern(v: str) -> str:
    # Filtering exclude uses glob-like patterns (e.g., "**/.git/**", "**/*.lock")
    # which are not strict regex. We accept them if they are valid as either regex
    # or glob. For glob we use fnmatch.translate to test.
    try:
        re.compile(v)
        return v
    except re.error:
        pass
    # Try as glob
    try:
        import fnmatch

        # fnmatch.translate always succeeds for valid glob.
        # For "[unclosed", it produces invalid regex.
        translated = fnmatch.translate(v)
        re.compile(translated)
        if v.count("[") != v.count("]"):
            raise ValueError(f"invalid pattern {v!r}: unbalanced brackets")
        return v
    except Exception as e:
        raise ValueError(f"invalid filter pattern {v!r}: {e}") from e


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(default="chronotrace")
    data_dir: str = Field(default="~/.chronotrace/data", description="Data directory with tilde")
    repositories_dir: str = Field(default="~/.chronotrace/repositories")


class HistoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_commits: int | None = Field(default=None)
    decay_half_life_days: float = Field(default=365)

    @field_validator("max_commits")
    @classmethod
    def _check_max_commits(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"max_commits must be null or positive integer, got {v!r}")
        return v

    @field_validator("decay_half_life_days")
    @classmethod
    def _check_decay(cls, v):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"decay_half_life_days must be positive number, got {v!r}")
        fv = float(v)
        if fv <= 0:
            raise ValueError(f"decay_half_life_days must be > 0, got {v!r}")
        return fv


class FilteringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("exclude")
    @classmethod
    def _check_exclude(cls, v):
        for pat in v:
            _validate_filter_pattern(pat)
        return v


class BugFixLabelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_sensitive: bool = False
    patterns: list[str] = Field(default_factory=list)

    @field_validator("patterns")
    @classmethod
    def _check_patterns(cls, v):
        for pat in v:
            _validate_regex_field(pat)
        return v


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    language: str = Field(default="python")
    granularity: str = Field(default="file")
    include_tests: bool = True
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    filtering: FilteringConfig = Field(default_factory=FilteringConfig)
    bug_fix_labels: BugFixLabelsConfig = Field(default_factory=BugFixLabelsConfig)

    @field_validator("language")
    @classmethod
    def _check_language(cls, v):
        return validate_language(v)

    @field_validator("granularity")
    @classmethod
    def _check_granularity(cls, v):
        return validate_granularity(v)


class GraphConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(default="neo4j")
    uri: str = Field(default="bolt://localhost:7687")
    database: str = Field(default="neo4j")
    batch_size: int = Field(default=500)

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v):
        return validate_graph_provider(v)

    @field_validator("batch_size")
    @classmethod
    def _check_batch(cls, v):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"graph.batch_size must be > 0, got {v!r}")
        return v


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(default="meta")
    model: str = Field(default="muse-spark-1.3")
    reasoning_effort: str = Field(default="medium")

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v):
        return validate_agent_provider(v)

    @field_validator("reasoning_effort")
    @classmethod
    def _check_effort(cls, v):
        return validate_reasoning_effort(v)


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8001)
    transport: str = Field(default="streamable-http")

    @field_validator("host")
    @classmethod
    def _check_host(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("mcp.host must be a non-empty string")
        return v.strip()

    @field_validator("port")
    @classmethod
    def _check_port(cls, v):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"mcp.port must be an integer, got {v!r}")
        if v < 0 or v > 65535:
            raise ValueError(f"mcp.port must be 0-65535, got {v}")
        return v

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v):
        if v not in ("streamable-http", "sse", "stdio"):
            raise ValueError(f"mcp.transport must be streamable-http|sse|stdio, got {v!r}")
        return v


class ChronoTraceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = Field(default=1)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    # Resolved absolute paths (populated by loader after validation)
    # We store them as private attributes? For now keep as properties.
    # Loader will mutate via object.__setattr__ if frozen.
