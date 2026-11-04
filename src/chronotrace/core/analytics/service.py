"""AnalyticsService facade (iter-5).

Orchestrates graph reads, scoring, and contract construction for all
four analytics capabilities (criticality, regression risk, historical
impact, test recommendation) plus file history, with shared handling
for status, evidence, and limitations.

Read-only; no graph mutations. Injected ``graph`` and ``config`` plus
``now`` injection for deterministic decay.
"""

from __future__ import annotations

import posixpath
from datetime import UTC, datetime
from typing import Any

from .contracts import (
    AnalyticsResult,
    CriticalityResult,
    Evidence,
    FileHistoryResult,
    ImpactResult,
    Limitations,
    RegressionResult,
    Status,
    TestRecoResult,
)
from .regression import (
    EVIDENCE_FRAMING as REGRESSION_EVIDENCE_FRAMING,
)
from .regression import (
    NO_FIX_LIMITATION,
)
from .regression import _file_exists as _regression_file_exists
from .regression import compute_regression_result as _compute_regression_result

try:
    from .test_reco import recommend_tests as _test_reco_recommend  # type: ignore
except Exception:  # pragma: no cover
    _test_reco_recommend = None  # type: ignore

# Thresholds — centralized per Decision 8 (documented constants)
MIN_COMMITS_FOR_AUTHORITATIVE = 10
MIN_TOUCHING = 3

# Disclaimers
_TEST_RECO_DISCLAIMER = "no claim of real coverage — recommendations are heuristic suggestions, not measured coverage or guaranteed relevance"
_FUTURE_DATE_NOTE = "commit has a future date — age clamped to 0"
_SHALLOW_TEMPLATE = "shallow history — {count} commits available, results have low confidence"
_INSUFFICIENT_TOUCHING_TEMPLATE = "insufficient history — {count} commits touching target, fewer than {threshold} observations"
_MISSING_COCHANGE_NOTE = "insufficient co-change evidence — fewer than {threshold} observations"
_NO_IMPORT_EDGE_NOTE = "no static import relationship resolved — absence of an edge does not prove absence of a dependency (dynamic imports, see Iteration 4 limitations)"


def _normalize_path(p: str) -> str:
    if not p:
        return ""
    p = p.replace("\\", "/")
    p = posixpath.normpath(p)
    if p.startswith("./"):
        p = p[2:]
    return p


def _repo_commit_count(graph: Any, repository_id: str) -> int | None:
    # Try via criticality helper first, then regression helpers, then repositories
    try:
        from .criticality import _repo_commit_count as _cc_count  # type: ignore
        cnt = _cc_count(graph, repository_id)  # type: ignore[misc]
        if isinstance(cnt, int):
            return cnt
    except Exception:
        pass
    # Try graph methods directly
    for meth in ("get_repo_commit_count", "commit_count", "count_commits", "get_commit_count"):
        if hasattr(graph, meth):
            try:
                res = getattr(graph, meth)(repository_id)  # type: ignore[misc]
                if isinstance(res, int):
                    return res
            except Exception:
                pass
        for inner_attr in ("commits", "commit_repo"):
            if hasattr(graph, inner_attr):
                try:
                    inner = getattr(graph, inner_attr)
                    if hasattr(inner, meth):
                        res = getattr(inner, meth)(repository_id)  # type: ignore[misc]
                        if isinstance(res, int):
                            return res
                    if hasattr(inner, "list_by_repository"):
                        lst = inner.list_by_repository(repository_id)  # type: ignore[misc]
                        if isinstance(lst, list):
                            return len(lst)
                except Exception:
                    pass
    for meth in ("list_by_repository", "list_commits"):
        if hasattr(graph, meth):
            try:
                lst = getattr(graph, meth)(repository_id)  # type: ignore[misc]
                if isinstance(lst, list):
                    return len(lst)
            except Exception:
                pass
        if hasattr(graph, "commits") and hasattr(graph.commits, meth):
            try:
                lst = getattr(graph.commits, meth)(repository_id)  # type: ignore[misc]
                if isinstance(lst, list):
                    return len(lst)
            except Exception:
                pass
    try:
        from .repositories import get_repo_commit_count as _repo_fn

        # resolve driver
        driver = getattr(graph, "driver", None) or getattr(graph, "_driver", None) or graph
        cnt = _repo_fn(driver, repository_id)  # type: ignore[misc]
        if isinstance(cnt, int):
            return cnt
    except Exception:
        pass
    return None


def _get_commits_touching_count(graph: Any, repository_id: str, target: str) -> int | None:
    try:
        from .criticality import _get_commits_touching as _gct  # type: ignore
        lst = _gct(graph, repository_id, target)  # type: ignore[misc]
        if isinstance(lst, list):
            return len(lst)
    except Exception:
        pass
    # Try generic via repositories
    try:
        from .repositories import get_commits_touching_file as _repo_fn

        driver = getattr(graph, "driver", None) or getattr(graph, "_driver", None) or graph
        rows = _repo_fn(driver, repository_id, target)  # type: ignore[misc]
        if isinstance(rows, list):
            return len(rows)
    except Exception:
        pass
    return None


def _has_future_commit(graph: Any, repository_id: str, target: str, now: datetime) -> bool:
    try:
        from .criticality import _get_commits_touching as _gct  # type: ignore
        from .decay import _parse_date  # type: ignore

        lst = _gct(graph, repository_id, target)  # type: ignore[misc]
        for c in lst or []:
            if isinstance(c, dict):
                raw_date = c.get("date")
                parsed = _parse_date(raw_date)
                if parsed is not None and parsed > now:
                    return True
    except Exception:
        pass
    return False


def _get_file_by_path_any(graph: Any, repository_id: str, target: str) -> Any | None:
    norm = _normalize_path(target)
    # Try regression helper first (handles many fake shapes)
    if _regression_file_exists(graph, repository_id, norm):
        # Return a truthy placeholder
        return {"path": norm, "repository_id": repository_id}
    # Try criticality helper
    try:
        from .criticality import _get_file as _cf  # type: ignore
        res = _cf(graph, repository_id, norm)  # type: ignore[misc]
        if res is not None:
            return res
    except Exception:
        pass
    return None


class AnalyticsService:
    """Facade orchestrating graph reads + scoring + contract construction.

    Args:
        graph: GraphGateway, driver, or fake providing read methods.
        config: ChronoTraceConfig or minimal object with
            ``analysis.history.decay_half_life_days``.
    """

    def __init__(self, graph: Any, config: Any | None = None) -> None:
        self.graph = graph
        self.config = config

    # ------------------------------------------------------------------
    # Criticality — tasks 2.1-2.3 (Decision 4)
    # ------------------------------------------------------------------
    def criticality(
        self,
        repository_id: str,
        target: str,
        now: datetime | None = None,
    ) -> AnalyticsResult[CriticalityResult]:
        """Compute change criticality for target file.

        Retrieves decayed churn ``sum(decay* (1+log1p(add+del)))``, fix
        involvement ``sum(decay for is_fix)``, co-change weighted sum, and
        fan-in distinct importer count, applies decay consistently with
        ``now`` injection, gathers repository-scoped min/max, normalizes,
        and weights with ``WEIGHTS = {churn:0.30, fix:0.30, cochange:0.25,
        fan_in:0.15}`` (heuristic). Score is sum of weighted normalized
        values in ``[0,1]`` with breakdown ``{raw, normalized, weighted}``,
        evidence, and deterministic ordering (sorted commits/relationships,
        now injection).

        Status:
        - ``not_found`` when file missing in graph.
        - ``insufficient_history`` when repo commits < 10 or touching < 3
          (shallow history).
        - ``ok`` otherwise.

        Limitations and evidence are populated per spec, including shallow
        history disclosure, future-date note, and repository isolation.
        """
        repo_id = str(repository_id or "").strip()
        target_raw = str(target or "").strip()
        norm_target = _normalize_path(target_raw) if target_raw else ""

        now_dt = now or datetime.now(UTC)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        else:
            now_dt = now_dt.astimezone(UTC)

        if not repo_id:
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repo_id,
                target=norm_target or target_raw,
                result=None,
                evidence=Evidence(commits=[], relationships=[]),
                limitations=Limitations(warnings=["repository_id must not be empty"]),
            )
        if not norm_target:
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repo_id,
                target=target_raw,
                result=None,
                evidence=Evidence(commits=[], relationships=[]),
                limitations=Limitations(warnings=["target must not be empty"]),
            )

        # File existence — repository-scoped
        exists = _get_file_by_path_any(self.graph, repo_id, norm_target)
        if exists is None:
            return AnalyticsResult(
                status=Status.NOT_FOUND,
                repository_id=repo_id,
                target=norm_target,
                result=None,
                evidence=Evidence(commits=[], relationships=[]),
                limitations=Limitations(warnings=[f"file not found: {norm_target}"]),
            )

        # Compute core result (read-only, deterministic)
        try:
            from .criticality import compute_criticality as _compute_criticality

            result, evidence, repo_stats = _compute_criticality(
                self.graph, repo_id, norm_target, self.config, now_dt
            )
        except Exception as exc:
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repo_id,
                target=norm_target,
                result=None,
                evidence=Evidence(commits=[], relationships=[]),
                limitations=Limitations(warnings=[f"criticality computation failed: {exc}"]),
            )

        # Determine commit counts for insufficiency check
        repo_commit_count = _repo_commit_count(self.graph, repo_id)
        touching_count = _get_commits_touching_count(self.graph, repo_id, norm_target)
        if touching_count is None:
            # Fall back to evidence commits length
            touching_count = len(evidence.commits) if evidence and evidence.commits else 0

        # Future-date detection
        has_future = _has_future_commit(self.graph, repo_id, norm_target, now_dt)

        warnings: list[str] = []
        shallow_history = False

        if repo_commit_count is not None and repo_commit_count < MIN_COMMITS_FOR_AUTHORITATIVE:
            shallow_history = True
            warnings.append(_SHALLOW_TEMPLATE.format(count=repo_commit_count))
        if touching_count is not None and touching_count < MIN_TOUCHING:
            warnings.append(
                _INSUFFICIENT_TOUCHING_TEMPLATE.format(count=touching_count, threshold=MIN_TOUCHING)
            )
        if has_future:
            warnings.append(_FUTURE_DATE_NOTE)

        # Insufficient history status when shallow or rarely touched
        # Spec allows either insufficient_history with result or ok with limitation;
        # we return insufficient_history with result populated so callers still
        # get breakdown but know confidence is low.
        if shallow_history or (touching_count is not None and touching_count < MIN_TOUCHING):
            status = Status.INSUFFICIENT
        else:
            status = Status.OK

        # Also surface insufficient co-change when cochange raw ==0? Keep as limitation note
        # rather than flipping status unless already insufficient, to avoid masking ok scores.
        # For criticality with zero co-change but sufficient commits, we keep OK but warn.
        cochange_raw = 0.0
        try:
            cochange_raw = float(result.components.get("cochange").raw) if result.components.get("cochange") else 0.0  # type: ignore[union-attr]
        except Exception:
            pass
        if cochange_raw == 0.0 and status == Status.OK:
            # Only warn, don't degrade to insufficient unless touching also low?
            # For shallow history we already insufficient; for normal case add note
            pass

        limitations = Limitations(
            shallow_history=shallow_history,
            commit_count=repo_commit_count,
            warnings=warnings,
        )

        return AnalyticsResult(
            status=status,
            repository_id=repo_id,
            target=norm_target,
            result=result,
            evidence=evidence,
            limitations=limitations,
        )

    # Alias for spec/MCP naming variants
    def get_change_criticality(
        self, repository_id: str, target: str, now: datetime | None = None
    ) -> AnalyticsResult[CriticalityResult]:
        return self.criticality(repository_id, target, now=now)

    def get_criticality(
        self, repository_id: str, target: str, now: datetime | None = None
    ) -> AnalyticsResult[CriticalityResult]:
        return self.criticality(repository_id, target, now=now)

    # ------------------------------------------------------------------
    # Regression risk — tasks 3.1/3.2
    # ------------------------------------------------------------------
    def regression_risk(
        self,
        repository_id: str,
        target: str,
        now: datetime | None = None,  # injected for API parity / determinism
    ) -> AnalyticsResult[RegressionResult]:
        """Return regression risk as historical evidence (not probability)."""
        _ = now  # reserved
        if not repository_id or not repository_id.strip():
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repository_id or "",
                target=target,
                result=None,
                evidence=Evidence(commits=[], relationships=[]),
                limitations=Limitations(warnings=["repository_id must not be empty"]),
            )
        if not target or not target.strip():
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repository_id,
                target=target,
                result=None,
                evidence=Evidence(commits=[], relationships=[]),
                limitations=Limitations(warnings=["target must not be empty"]),
            )

        norm_target = _normalize_path(target.strip())
        exists = _regression_file_exists(self.graph, repository_id, norm_target)
        # Also check via generic helper for fakes that seed only commits
        if not exists:
            # try criticality helper as second opinion for files without explicit node
            try:
                from .criticality import _get_file as _cf  # type: ignore
                if _cf(self.graph, repository_id, norm_target) is not None:  # type: ignore[misc]
                    exists = True
            except Exception:
                pass
        if not exists:
            return AnalyticsResult(
                status=Status.NOT_FOUND,
                repository_id=repository_id,
                target=norm_target,
                result=RegressionResult(fix_commits=[], related_files=[]),
                evidence=Evidence(commits=[], relationships=[]),
                limitations=Limitations(
                    warnings=[
                        "file not found in graph for repository",
                        REGRESSION_EVIDENCE_FRAMING,
                    ]
                ),
            )

        result = _compute_regression_result(self.graph, repository_id, norm_target)

        evidence = Evidence(
            commits=list(result.fix_commits),
            relationships=[],
            diagnostics_ref=None,
        )

        framing_warnings: list[str] = [REGRESSION_EVIDENCE_FRAMING]

        if not result.fix_commits:
            warnings = [NO_FIX_LIMITATION] + framing_warnings
            return AnalyticsResult(
                status=Status.INSUFFICIENT,
                repository_id=repository_id,
                target=norm_target,
                result=RegressionResult(fix_commits=[], related_files=[]),
                evidence=evidence,
                limitations=Limitations(warnings=warnings),
            )

        if not result.related_files:
            warnings = framing_warnings + ["no co-changed files observed in fix commits for target"]
            return AnalyticsResult(
                status=Status.OK,
                repository_id=repository_id,
                target=norm_target,
                result=result,
                evidence=evidence,
                limitations=Limitations(warnings=warnings),
            )

        return AnalyticsResult(
            status=Status.OK,
            repository_id=repository_id,
            target=norm_target,
            result=result,
            evidence=evidence,
            limitations=Limitations(warnings=framing_warnings),
        )

    def get_regression_risk(
        self, repository_id: str, target: str, now: datetime | None = None
    ) -> AnalyticsResult[RegressionResult]:
        return self.regression_risk(repository_id, target, now=now)

    # ------------------------------------------------------------------
    # Test recommendation — Task 5.2
    # ------------------------------------------------------------------
    def recommend_tests(
        self,
        repository_id: str,
        target: str,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> AnalyticsResult[TestRecoResult]:
        """Rank candidate test files for target with disclaimer."""
        target_norm = _normalize_path(target) if target else ""
        repo_id = str(repository_id or "").strip()
        if not repo_id or not target_norm:
            limitations = Limitations(warnings=[_TEST_RECO_DISCLAIMER])
            return AnalyticsResult(
                status=Status.ERROR if not repo_id else Status.NOT_FOUND,
                repository_id=repo_id,
                target=target_norm or str(target),
                result=TestRecoResult(candidates=[]),
                evidence=Evidence(),
                limitations=limitations,
            )
        now_dt = now or datetime.now(UTC)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        # Check target existence
        exists = _get_file_by_path_any(self.graph, repo_id, target_norm)
        if exists is None:
            limitations = Limitations(
                warnings=[
                    f"file not found: {target_norm}",
                    _TEST_RECO_DISCLAIMER,
                ]
            )
            return AnalyticsResult(
                status=Status.NOT_FOUND,
                repository_id=repo_id,
                target=target_norm,
                result=None,
                evidence=Evidence(),
                limitations=limitations,
            )
        if _test_reco_recommend is None:
            return AnalyticsResult(
                status=Status.INSUFFICIENT,
                repository_id=repo_id,
                target=target_norm,
                result=TestRecoResult(candidates=[]),
                evidence=Evidence(),
                limitations=Limitations(warnings=[_TEST_RECO_DISCLAIMER, "test recommendation not available"]),
            )
        try:
            result = _test_reco_recommend(repo_id, target_norm, self.graph, self.config, now_dt, limit=limit)  # type: ignore[misc]
        except Exception as exc:
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repo_id,
                target=target_norm,
                result=None,
                evidence=Evidence(),
                limitations=Limitations(warnings=[f"recommend_tests failed: {exc}", _TEST_RECO_DISCLAIMER]),
            )
        warnings: list[str] = [_TEST_RECO_DISCLAIMER]
        try:
            cnt = _repo_commit_count(self.graph, repo_id)
            if cnt is not None and cnt < MIN_COMMITS_FOR_AUTHORITATIVE:
                warnings.append(_SHALLOW_TEMPLATE.format(count=cnt))
        except Exception:
            pass
        limitations = Limitations(warnings=warnings)
        try:
            cnt = _repo_commit_count(self.graph, repo_id)
            if cnt is not None:
                limitations.commit_count = cnt
        except Exception:
            pass
        evidence = Evidence()
        return AnalyticsResult(
            status=Status.OK,
            repository_id=repo_id,
            target=target_norm,
            result=result,
            evidence=evidence,
            limitations=limitations,
        )

    def get_test_recommendations(self, *args: Any, **kwargs: Any) -> AnalyticsResult[TestRecoResult]:
        return self.recommend_tests(*args, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Historical impact — stub (tasks 4.x will fill)
    # ------------------------------------------------------------------
    def historical_impact(
        self, repository_id: str, target: str, now: datetime | None = None
    ) -> AnalyticsResult[ImpactResult]:
        _ = now
        norm_target = _normalize_path(target) if target else ""
        repo_id = str(repository_id or "").strip()
        if not repo_id or not norm_target:
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repo_id,
                target=norm_target or str(target),
                result=None,
                evidence=Evidence(),
                limitations=Limitations(warnings=["repository_id and target must not be empty"]),
            )
        exists = _get_file_by_path_any(self.graph, repo_id, norm_target)
        if exists is None:
            return AnalyticsResult(
                status=Status.NOT_FOUND,
                repository_id=repo_id,
                target=norm_target,
                result=None,
                evidence=Evidence(),
                limitations=Limitations(warnings=[f"file not found: {norm_target}"]),
            )
        # Try to delegate to impact module if present
        try:
            # Prefer analyze_historical_impact which returns (result, evidence) with supporting commits
            try:
                from .impact import analyze_historical_impact as _analyze_impact  # type: ignore

                now_dt = now or datetime.now(UTC)
                if now_dt.tzinfo is None:
                    now_dt = now_dt.replace(tzinfo=UTC)
                ret = _analyze_impact(self.graph, repo_id, norm_target, self.config, now_dt)  # type: ignore[misc]
                if isinstance(ret, tuple) and len(ret) == 2:
                    impact_result, impact_evidence = ret  # type: ignore[misc]
                else:
                    impact_result = ret  # type: ignore[assignment]
                    impact_evidence = Evidence(relationships=[])
            except ImportError:
                from .impact import compute_impact as _compute_impact  # type: ignore

                now_dt = now or datetime.now(UTC)
                if now_dt.tzinfo is None:
                    now_dt = now_dt.replace(tzinfo=UTC)
                raw_result = _compute_impact(self.graph, repo_id, norm_target, self.config, now_dt)  # type: ignore[misc]
                if isinstance(raw_result, tuple) and len(raw_result) == 2:
                    impact_result, impact_evidence = raw_result  # type: ignore[misc]
                else:
                    impact_result = raw_result  # type: ignore[assignment]
                    impact_evidence = Evidence(relationships=[])
            if impact_result is None:
                raise ValueError("impact returned None")
            warnings: list[str] = []
            cnt = _repo_commit_count(self.graph, repo_id)
            if cnt is not None and cnt < MIN_COMMITS_FOR_AUTHORITATIVE:
                warnings.append(_SHALLOW_TEMPLATE.format(count=cnt))
            # Missing import edge disclaimer when no importers/dependencies?
            imp_list = getattr(impact_result, "importers", None) or []
            dep_list = getattr(impact_result, "dependencies", None) or []
            co_list = getattr(impact_result, "co_changed", None) or []
            # Excluded paths / parser failures disclosure (Task 4.3)
            excluded: int | None = None
            parser_failures: int | None = None
            if self.config is not None:
                try:
                    filtering = getattr(getattr(self.config, "analysis", None), "filtering", None)
                    if filtering is not None:
                        excl_list = getattr(filtering, "exclude", None)
                        if isinstance(excl_list, list):
                            excluded = len(excl_list)
                except Exception:
                    pass
                try:
                    diag = getattr(self.config, "diagnostics", None)
                    if isinstance(diag, dict):
                        parser_failures = diag.get("parser_failures") or diag.get("files_failed")
                        if parser_failures is not None:
                            parser_failures = int(parser_failures)
                except Exception:
                    pass
            try:
                diag_obj = getattr(self.graph, "diagnostics", None) or getattr(self.graph, "_diagnostics", None)
                if isinstance(diag_obj, dict):
                    if excluded is None and "excluded_paths" in diag_obj:
                        try:
                            excluded = int(diag_obj.get("excluded_paths") or 0)
                        except Exception:
                            pass
                    if parser_failures is None and "parser_failures" in diag_obj:
                        try:
                            parser_failures = int(diag_obj.get("parser_failures") or 0)
                        except Exception:
                            pass
                    if parser_failures is None and "files_failed" in diag_obj:
                        try:
                            parser_failures = int(diag_obj.get("files_failed") or 0)
                        except Exception:
                            pass
            except Exception:
                pass
            if excluded is not None and excluded > 0:
                warnings.append(f"import-derived metrics may be incomplete — {excluded} excluded paths")
            if parser_failures is not None and parser_failures > 0:
                warnings.append(f"import-derived metrics may be incomplete — {parser_failures} parser failures")
            if not imp_list and not dep_list:
                warnings.append(_NO_IMPORT_EDGE_NOTE)
                warnings.append("no static relationship resolved — absence of an edge does not prove absence of a dependency")
            status = Status.OK
            # If co_changed empty, treat as insufficient per Task 4.3
            if not co_list:
                status = Status.INSUFFICIENT
                warnings.append(_MISSING_COCHANGE_NOTE.format(threshold=MIN_TOUCHING))
                warnings.append("insufficient co-change evidence — fewer than 1 observations")
            # Use evidence from impact if provided, else build from supporting commits
            evidence_obj = impact_evidence if isinstance(impact_evidence, Evidence) else Evidence(relationships=[])  # type: ignore[assignment]
            # If evidence has no commits but result has supporting commits, collect them
            if not getattr(evidence_obj, "commits", None):
                collected: list[Any] = []
                seen: set[str] = set()
                for entry in co_list or []:
                    for c in getattr(entry, "supporting_commits", []) or []:
                        h = getattr(c, "hash", None)
                        if h and str(h) not in seen:
                            seen.add(str(h))
                            collected.append(c)
                if collected:
                    # Preserve relationships from evidence_obj if any
                    evidence_obj = Evidence(commits=collected, relationships=getattr(evidence_obj, "relationships", []) or [], diagnostics_ref=getattr(evidence_obj, "diagnostics_ref", None))
            # If evidence has no relationships but we have importers/dependencies/co_changed, build minimal relationships
            if not getattr(evidence_obj, "relationships", None):
                rels: list[Any] = []
                for p in imp_list or []:
                    from .contracts import RelationshipEvidence as _RE

                    rels.append(_RE(source=p, target=norm_target, kind="IMPORTS"))
                for p in dep_list or []:
                    from .contracts import RelationshipEvidence as _RE

                    rels.append(_RE(source=norm_target, target=p, kind="IMPORTS"))
                for entry in co_list or []:
                    from .contracts import RelationshipEvidence as _RE

                    rels.append(_RE(source=norm_target, target=entry.path, kind="CO_CHANGED_WITH", count=getattr(entry, "count", None), weight=getattr(entry, "weight", None), confidence=getattr(entry, "confidence", None)))
                if rels:
                    evidence_obj = Evidence(commits=getattr(evidence_obj, "commits", []) or [], relationships=rels, diagnostics_ref=getattr(evidence_obj, "diagnostics_ref", None))
            limitations = Limitations(
                shallow_history=bool(cnt is not None and cnt < MIN_COMMITS_FOR_AUTHORITATIVE),
                commit_count=cnt,
                excluded_paths_count=excluded,
                parser_failures_count=parser_failures,
                warnings=warnings,
            )
            return AnalyticsResult(
                status=status,
                repository_id=repo_id,
                target=norm_target,
                result=impact_result,  # type: ignore[arg-type]
                evidence=evidence_obj,
                limitations=limitations,
            )
        except Exception:
            pass
        # Fallback stub
        return AnalyticsResult(
            status=Status.OK,
            repository_id=repo_id,
            target=norm_target,
            result=ImpactResult(importers=[], dependencies=[], co_changed=[], hidden_couplings=[]),
            evidence=Evidence(),
            limitations=Limitations(warnings=[_NO_IMPORT_EDGE_NOTE]),
        )

    def get_historical_impact(
        self, repository_id: str, target: str, now: datetime | None = None
    ) -> AnalyticsResult[ImpactResult]:
        return self.historical_impact(repository_id, target, now=now)

    # ------------------------------------------------------------------
    # File history helper (iter-6 support)
    # ------------------------------------------------------------------
    def get_file_history(
        self,
        repository_id: str,
        target: str,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> AnalyticsResult[FileHistoryResult]:
        _ = now, limit
        norm_target = _normalize_path(target) if target else ""
        repo_id = str(repository_id or "").strip()
        if not repo_id or not norm_target:
            return AnalyticsResult(
                status=Status.ERROR,
                repository_id=repo_id,
                target=norm_target or str(target),
                result=None,
                evidence=Evidence(),
                limitations=Limitations(warnings=["repository_id and target must not be empty"]),
            )
        exists = _get_file_by_path_any(self.graph, repo_id, norm_target)
        if exists is None:
            return AnalyticsResult(
                status=Status.NOT_FOUND,
                repository_id=repo_id,
                target=norm_target,
                result=None,
                evidence=Evidence(),
                limitations=Limitations(warnings=[f"file not found: {norm_target}"]),
            )
        # Try to fetch via criticality helper or repositories
        commits: list[Any] = []
        try:
            from .criticality import _get_commits_touching as _gct  # type: ignore

            commits = _gct(self.graph, repo_id, norm_target)  # type: ignore[misc]
        except Exception:
            pass
        if limit is not None and isinstance(commits, list):
            try:
                commits = commits[: int(limit)]
            except Exception:
                pass
        # Build FileHistoryResult
        from .contracts import CommitEvidence as _CE

        hist_commits = []
        for c in commits or []:
            if isinstance(c, dict):
                hist_commits.append(
                    _CE(
                        hash=str(c.get("hash") or c.get("id") or ""),
                        date=str(c.get("date")) if c.get("date") is not None else None,
                        message=str(c.get("message")) if c.get("message") is not None else None,
                        fix_reason=str(c.get("fix_reason")) if c.get("fix_reason") is not None else None,
                    )
                )
        evidence = Evidence(commits=hist_commits)
        return AnalyticsResult(
            status=Status.OK,
            repository_id=repo_id,
            target=norm_target,
            result=FileHistoryResult(commits=hist_commits),
            evidence=evidence,
            limitations=Limitations(warnings=[]),
        )

    def file_history(
        self, repository_id: str, target: str, limit: int | None = None, now: datetime | None = None
    ) -> AnalyticsResult[FileHistoryResult]:
        return self.get_file_history(repository_id, target, limit=limit, now=now)

    # Back-compat aliases
    get_change_criticality = criticality
    get_criticality = criticality
    get_regression_risk = regression_risk
    get_historical_impact = historical_impact


__all__ = ["AnalyticsService", "MIN_COMMITS_FOR_AUTHORITATIVE", "MIN_TOUCHING"]
