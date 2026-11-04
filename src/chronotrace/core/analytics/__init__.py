"""Analytics package — read-only deterministic helpers."""

try:
    from . import regression as regression  # noqa: F401
except ImportError:
    regression = None  # type: ignore

try:
    from . import service as service  # noqa: F401
except ImportError:
    service = None  # type: ignore

try:
    from . import repositories as repositories  # noqa: F401
except ImportError:
    repositories = None  # type: ignore[assignment]

__all__ = ["regression", "repositories", "service"]
