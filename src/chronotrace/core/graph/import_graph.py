"""Import graph repository — replace semantics for IMPORTS edges.

Re-exports canonical implementation from graph.repositories to provide
both import paths required by tasks.
"""

from __future__ import annotations

from chronotrace.graph.repositories.import_repo import ImportGraphRepository

__all__ = ["ImportGraphRepository"]
