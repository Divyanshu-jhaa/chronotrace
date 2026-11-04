"""Exponential time decay for analytics (iter-5, Decision 3).

Weight formula ``w = 0.5 ** (age_days / half_life_days)`` so a commit
one half-life old contributes half weight. Future-dated commits are
clamped to age 0 (weight 1.0). ``half_life_days`` is validated positive.
``now`` is injectable for determinism.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# Default half-life when no config is supplied (matches AnalysisConfig default).
DECAY_HALF_LIFE_DAYS: float = 365.0


def _parse_date(value: Any) -> datetime | None:
    """Parse a commit date value to a timezone-aware datetime.

    Accepts datetime objects (naive treated as UTC), ISO-8601 strings,
    or None. Returns None if value cannot be parsed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        # epoch seconds
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Handle common Neo4j / ISO formats: replace Z with +00:00
        iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
        # Try fromisoformat
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            pass
        # Try parsing without microseconds
        for fmt in (
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                return dt
            except Exception:
                continue
        return None
    return None


def age_days(commit_date: Any, now: datetime | None = None) -> float:
    """Compute age in days between commit_date and now.

    Both dates are normalized to UTC. If commit_date is in the future
    relative to now, age is clamped to 0.0 (future-date handling is
    completed in :func:`decay_weight` as well).

    Args:
        commit_date: datetime or ISO string or epoch, or None.
        now: reference datetime (default utcnow, timezone-aware).

    Returns:
        Non-negative float age in days. Returns 0.0 if commit_date is
        None, unparseable, or in the future.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    dt = _parse_date(commit_date)
    if dt is None:
        return 0.0
    delta_seconds = (now - dt).total_seconds()
    if delta_seconds <= 0:
        return 0.0
    return delta_seconds / 86400.0


def decay_weight(age_days_value: float, half_life_days: float) -> float:
    """Compute exponential decay weight.

    Formula ``0.5 ** (age / half_life)``.

    Args:
        age_days_value: age in days (negative clamped to 0).
        half_life_days: positive half-life in days.

    Raises:
        ValueError: if half_life_days <= 0 or non-numeric.

    Returns:
        Weight in (0, 1] — age 0 => 1.0, age == half_life => 0.5,
        age == 2*half_life => 0.25. Future dates (age <=0) => 1.0.
    """
    # Validate half_life
    try:
        hl = float(half_life_days)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"decay_half_life_days must be positive number, got {half_life_days!r}") from exc
    if not math.isfinite(hl) or hl <= 0:
        raise ValueError(f"decay_half_life_days must be > 0, got {half_life_days!r}")
    # Validate age
    try:
        age = float(age_days_value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"age_days must be numeric, got {age_days_value!r}") from exc
    if not math.isfinite(age):
        raise ValueError(f"age_days must be finite, got {age_days_value!r}")
    age = max(0.0, age)
    return float(0.5 ** (age / hl))


def get_half_life(config: Any | None) -> float:
    """Resolve half-life from config or default.

    Reads ``config.analysis.history.decay_half_life_days`` with
    validation. Falls back to :data:`DECAY_HALF_LIFE_DAYS` when config
    is None or missing. Invalid values raise ValueError (mirroring
    Pydantic validation for the YAML config).

    Args:
        config: ChronoTraceConfig-like object, dict, or None.

    Returns:
        Positive float half-life days.

    Raises:
        ValueError: if resolved value is not positive.
    """
    raw: Any = None
    if config is None:
        return float(DECAY_HALF_LIFE_DAYS)
    # Try attribute access: config.analysis.history.decay_half_life_days
    try:
        if hasattr(config, "analysis"):
            ana: Any = getattr(config, "analysis")
            if hasattr(ana, "history"):
                hist: Any = getattr(ana, "history")
                if hasattr(hist, "decay_half_life_days"):
                    raw = getattr(hist, "decay_half_life_days")
                elif isinstance(hist, dict):
                    raw = hist.get("decay_half_life_days")
            elif isinstance(ana, dict):
                hist = ana.get("history", {})
                if isinstance(hist, dict):
                    raw = hist.get("decay_half_life_days")
        # Direct history
        if raw is None and hasattr(config, "history"):
            hist = getattr(config, "history")
            if hasattr(hist, "decay_half_life_days"):
                raw = getattr(hist, "decay_half_life_days")
        # Dict-like config
        if raw is None and isinstance(config, dict):
            raw = config.get("decay_half_life_days")
            if raw is None and "analysis" in config:
                ana = config["analysis"]
                if isinstance(ana, dict):
                    hist = ana.get("history", {})
                    if isinstance(hist, dict):
                        raw = hist.get("decay_half_life_days")
        if raw is None:
            # Also try flat config key
            if isinstance(config, dict):
                raw = config.get("analysis.history.decay_half_life_days")
            if raw is None and hasattr(config, "decay_half_life_days"):
                raw = getattr(config, "decay_half_life_days")
        if raw is None:
            return float(DECAY_HALF_LIFE_DAYS)
        hl = float(raw)
        if not math.isfinite(hl) or hl <= 0:
            raise ValueError(f"decay_half_life_days must be > 0, got {raw!r}")
        return hl
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"decay_half_life_days must be positive number, got {raw!r}") from exc


__all__ = ["DECAY_HALF_LIFE_DAYS", "age_days", "decay_weight", "get_half_life"]
