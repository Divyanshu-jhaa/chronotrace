"""Shim re-exporting ImportGraphRepository for core path compatibility."""

from __future__ import annotations

# Re-export canonical implementation from graph.repositories
from chronotrace.graph.repositories.import_repo import ImportGraphRepository  # noqa: F401

__all__ = ["ImportGraphRepository"]
