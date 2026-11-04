"""Repository-scoped min-max normalization for analytics.

This module implements the normalization step described in
``openspec/changes/iter-5-analytics/design.md`` Decision 4:

* Normalization is **repository-scoped** — each repository's own
  ``min``/``max`` distribution is used, never a global constant.
  Supplying different ``repo_stats`` for different repositories
  guarantees cross-repo isolation: identical raw values normalize
  differently when the repositories have different ranges.

* Formula: ``norm = (raw - min) / (max - min)`` when ``max > min``,
  else ``0.5`` (documented alternative ``0.0`` — this implementation
  chooses ``0.5`` as the neutral midpoint so a single-file repository
  or a repository where all files share the identical raw value does
  not collapse to ``0``; callers that prefer ``0.0`` can map the
  fallback explicitly).

* Single-file repository is the degenerate case of ``max == min``
  (only one observation) and therefore returns ``0.5`` for that file
  per the rule above.

* Cross-repo isolation is achieved by requiring the caller to supply
  per-repository statistics; the functions themselves are pure and
  stateless.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, overload


def _normalize_value(raw: float, v_min: float, v_max: float) -> float:
    """Normalize a single value with repository-scoped min-max.

    Returns ``(raw - min) / (max - min)`` when ``max > min``, else ``0.5``.

    The ``0.5`` fallback is documented; an alternative ``0.0`` would also
    avoid divide-by-zero but would bias single-value distributions to the
    minimum. ``0.5`` is the neutral midpoint and matches the design's
    ``0.5 or 0.0 documented`` guidance.
    """
    if v_max > v_min:
        return (raw - v_min) / (v_max - v_min)
    # Identical values / single-file repo: avoid divide-by-zero.
    # Documented fallback is 0.5 (alternative 0.0 also valid per spec).
    return 0.5


def _extract_min_max(entry: Any) -> tuple[float, float] | None:
    """Extract (min, max) from a repo_stats entry.

    Accepted shapes (all repository-scoped):
    * ``(min, max)`` tuple/list of length 2
    * ``{"min": ..., "max": ...}`` dict
    * ``{"min_val": ..., "max_val": ...}`` dict (tolerant alias)
    * ``{"minimum": ..., "maximum": ...}`` dict (tolerant alias)
    * Object with ``min``/``max`` attributes
    * Object with ``min_val``/``max_val`` attributes

    Returns ``None`` if the shape is unrecognised so the caller can
    apply the documented ``0.5`` fallback.
    """
    if entry is None:
        return None
    # Tuple / list of 2
    if isinstance(entry, (list, tuple)):
        if len(entry) == 2:
            try:
                return float(entry[0]), float(entry[1])
            except (TypeError, ValueError):
                return None
        return None
    # Dict shapes
    if isinstance(entry, dict):
        # canonical
        if "min" in entry and "max" in entry:
            try:
                return float(entry["min"]), float(entry["max"])
            except (TypeError, ValueError):
                return None
        if "min_val" in entry and "max_val" in entry:
            try:
                return float(entry["min_val"]), float(entry["max_val"])
            except (TypeError, ValueError):
                return None
        if "minimum" in entry and "maximum" in entry:
            try:
                return float(entry["minimum"]), float(entry["maximum"])
            except (TypeError, ValueError):
                return None
        return None
    # Attribute access
    try:
        if hasattr(entry, "min") and hasattr(entry, "max"):
            return float(entry.min), float(entry.max)
        if hasattr(entry, "min_val") and hasattr(entry, "max_val"):
            return float(entry.min_val), float(entry.max_val)
    except (TypeError, ValueError, AttributeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@overload
def min_max_normalize(values: Sequence[float]) -> list[float]: ...


@overload
def min_max_normalize(values: Mapping[str, float]) -> dict[str, float]: ...


def min_max_normalize(
    values: Sequence[float] | Mapping[str, float],
) -> list[float] | dict[str, float]:
    """Repository-scoped min-max normalization for a collection of values.

    The min/max are derived **from the supplied collection itself**, so
    calling this separately per repository (with each repository's own
    values) yields repository-scoped normalization and cross-repo
    isolation automatically.

    Formula per element::

        norm = (v - min) / (max - min)  if max > min else 0.5

    The ``0.5`` fallback handles:

    * identical values (``max == min``)
    * single-file repository (single observation, degenerate range)
    * empty inputs are returned unchanged (empty list/dict)

    Args:
        values: Either a sequence of floats (e.g., ``[0, 5, 10]``) or a
            mapping of ``name -> raw`` (e.g., ``{"a.py": 3.0}``).  The
            mapping form preserves keys in the output for convenience
            when normalizing per-file raw components.

    Returns:
        Normalized values in ``[0, 1]`` (or ``0.5`` fallback), with the
        same container shape as the input: ``list`` for sequence input,
        ``dict`` for mapping input. Empty input yields empty output.
    """
    # Mapping path — preserve keys
    if isinstance(values, Mapping):
        if not values:
            return {}
        try:
            numeric_vals = [float(v) for v in values.values()]
        except (TypeError, ValueError) as exc:
            # If non-numeric, let it propagate as TypeError-like
            raise TypeError("min_max_normalize mapping values must be numeric") from exc
        v_min = min(numeric_vals)
        v_max = max(numeric_vals)
        if v_max > v_min:
            return {k: (float(v) - v_min) / (v_max - v_min) for k, v in values.items()}
        # identical values / single entry
        return {k: 0.5 for k in values}

    # Sequence path
    # Guard against string being treated as sequence
    if isinstance(values, (str, bytes)):
        raise TypeError("min_max_normalize expects a sequence of numbers, not a string")
    vals = list(values)  # type: ignore[arg-type]
    if not vals:
        return []
    try:
        numeric_vals = [float(v) for v in vals]
    except (TypeError, ValueError) as exc:
        raise TypeError("min_max_normalize values must be numeric") from exc
    v_min = min(numeric_vals)
    v_max = max(numeric_vals)
    if v_max > v_min:
        denom = v_max - v_min
        return [(float(v) - v_min) / denom for v in vals]
    return [0.5 for _ in vals]


def normalize_components(
    raw: Mapping[str, float],
    repo_stats: Mapping[str, Any],
) -> dict[str, float]:
    """Normalize per-component raw values using repository-scoped stats.

    This is the form used by criticality scoring: ``raw`` holds the
    four (or any) component raw values for a single file (e.g.,
    ``{"churn": 12, "fix": 2, "cochange": 5, "fan_in": 3}``), and
    ``repo_stats`` holds the repository's per-component ``min``/``max``
    distribution (e.g., ``{"churn": {"min": 0, "max": 20}, ...}``).

    Repository-scoped isolation: callers must supply ``repo_stats``
    computed from the target repository only. Two calls with identical
    ``raw`` but different ``repo_stats`` (different repositories) will
    produce different normalized results, satisfying cross-repo
    isolation without any global state.

    Formula per component::

        norm = (raw - min) / (max - min)  if max > min else 0.5

    Edge handling:

    * ``max == min`` (identical values across the repository, or
      single-file repository) → ``0.5`` per component.
    * Missing component in ``repo_stats`` → ``0.5`` (documented
      fallback; treats absent distribution as insufficient).
    * Unrecognised ``repo_stats`` entry shape → ``0.5``.
    * Empty ``raw`` → ``{}``.

    Args:
        raw: Mapping of ``component_name -> raw_value`` for the target
            file. Values are coerced to ``float``.
        repo_stats: Mapping of ``component_name -> stats`` where each
            stats entry is one of the shapes accepted by
            :func:`_extract_min_max` (tuple ``(min, max)`` or dict with
            ``min``/``max``). The stats must be pre-computed per
            repository to ensure isolation.

            A flat ``{"min": ..., "max": ...}`` ``repo_stats`` is also
            accepted when ``raw`` contains a single component — it is
            treated as the stats for that sole component (convenience
            for single-component callers).

    Returns:
        Dict of ``component_name -> normalized_value`` in ``[0, 1]`` or
        ``0.5`` fallback. Keys match ``raw`` exactly; ordering follows
        ``raw`` insertion order.
    """
    if not raw:
        return {}

    # Convenience: flat stats for single-component raw
    # e.g., raw={"churn": 5}, repo_stats={"min":0, "max":10}
    flat_fallback: tuple[float, float] | None = None
    if len(raw) == 1 and "min" in repo_stats and "max" in repo_stats:
        # Check that repo_stats is not per-component (i.e., keys are not component names)
        # If raw's sole key is not in repo_stats, treat repo_stats itself as the min/max.
        sole_key = next(iter(raw))
        if sole_key not in repo_stats:
            try:
                flat_fallback = (float(repo_stats["min"]), float(repo_stats["max"]))  # type: ignore[index]
            except (TypeError, ValueError, KeyError):
                flat_fallback = None

    result: dict[str, float] = {}
    for comp, raw_val in raw.items():
        try:
            rv = float(raw_val)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"raw value for component '{comp}' must be numeric") from exc

        # Resolve min/max
        min_max: tuple[float, float] | None = None
        if flat_fallback is not None:
            min_max = flat_fallback
        else:
            entry = repo_stats.get(comp) if isinstance(repo_stats, Mapping) else None
            min_max = _extract_min_max(entry) if entry is not None else None

        if min_max is None:
            # Missing or unrecognised stats → documented 0.5 fallback
            result[comp] = 0.5
            continue

        v_min, v_max = min_max
        result[comp] = _normalize_value(rv, v_min, v_max)

    return result


__all__ = ["min_max_normalize", "normalize_components"]
