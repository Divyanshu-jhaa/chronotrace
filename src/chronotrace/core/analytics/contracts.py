"""Shared result contracts for analytics (iter-5).

Implements Decision 2 common result contract with generic AnalyticsResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar


class Status(str, Enum):
    """Analytics status vocabulary."""

    OK = "ok"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INSUFFICIENT = "insufficient_history"
    NOT_FOUND = "not_found"
    ERROR = "error"

    # Back-compat aliases for string comparisons that use dotted names
    @property
    def value_str(self) -> str:
        return str(self.value)


# Aliases for spec string literal expectations
# Some consumers compare status == "ok" etc directly; Enum(str) handles that via ==.


@dataclass(slots=True)
class Limitations:
    shallow_history: bool = False
    commit_count: int | None = None
    excluded_paths_count: int | None = None
    parser_failures_count: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class CommitEvidence:
    hash: str
    date: str | None = None
    message: str | None = None
    fix_reason: str | None = None


@dataclass(slots=True, frozen=True)
class RelationshipEvidence:
    source: str
    target: str
    kind: str
    count: int | None = None
    weight: float | None = None
    confidence: float | None = None


@dataclass(slots=True)
class Evidence:
    commits: list[CommitEvidence] = field(default_factory=list)
    relationships: list[RelationshipEvidence] = field(default_factory=list)
    diagnostics_ref: str | None = None


T = TypeVar("T")


@dataclass(slots=True)
class AnalyticsResult(Generic[T]):
    status: Status
    repository_id: str
    target: str
    result: T | None
    evidence: Evidence
    limitations: Limitations


# ---------------------------------------------------------------------------
# Per-capability payloads
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TestRecoCandidate:
    """Individual ranked test candidate."""

    path: str
    score: float
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    # Optional detailed fields for richer consumers
    co_change_count: int = 0
    fix_count: int = 0
    imports_target: bool = False
    naming_match: bool = False


@dataclass(slots=True)
class TestRecoResult:
    candidates: list[TestRecoCandidate] = field(default_factory=list)


# Keep alias for spec naming variants
RankedTest = TestRecoCandidate


@dataclass(slots=True)
class CriticalityBreakdown:
    raw: float
    normalized: float
    weighted: float
    score: float


@dataclass(slots=True)
class CriticalityResult:
    score: float
    components: dict[str, CriticalityBreakdown] = field(default_factory=dict)
    repo_stats: dict = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RankedFile:
    path: str
    count: int
    confidence: float
    score: float | None = None


@dataclass(slots=True)
class RegressionResult:
    fix_commits: list[CommitEvidence] = field(default_factory=list)
    related_files: list[RankedFile] = field(default_factory=list)


@dataclass(slots=True)
class FlaggedCoChange:
    path: str
    count: int
    confidence: float | None = None
    weight: float | None = None
    potential_hidden_coupling: bool = False
    note: str | None = None
    supporting_commits: list[CommitEvidence] = field(default_factory=list)


@dataclass(slots=True)
class ImpactResult:
    importers: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    co_changed: list[FlaggedCoChange] = field(default_factory=list)
    hidden_couplings: list[FlaggedCoChange] = field(default_factory=list)


# Generic evidence for test reco (reused)
@dataclass(slots=True)
class FileHistoryResult:
    commits: list[CommitEvidence] = field(default_factory=list)


__all__ = [
    "AnalyticsResult",
    "CommitEvidence",
    "CriticalityBreakdown",
    "CriticalityResult",
    "Evidence",
    "FileHistoryResult",
    "FlaggedCoChange",
    "ImpactResult",
    "Limitations",
    "RankedFile",
    "RankedTest",
    "RegressionResult",
    "RelationshipEvidence",
    "Status",
    "TestRecoCandidate",
    "TestRecoResult",
]
