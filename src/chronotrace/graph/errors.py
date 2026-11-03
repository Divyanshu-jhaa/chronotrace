"""Domain error hierarchy for the graph layer.

All Neo4j driver exceptions are mapped to these types.
Messages never include credentials or raw Cypher with secrets.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base for all graph-layer domain errors."""

    pass


class GraphConnectionError(GraphError):
    """Neo4j is unreachable or connectivity verification failed."""

    pass


class GraphNotAvailable(GraphError):
    """Graph driver is closed or database is not available for operations."""

    pass


class GraphWriteError(GraphError):
    """A write operation failed (constraint, transient, or validation)."""

    pass


class GraphValidationError(GraphWriteError):
    """Repository isolation or input validation violated before Cypher execution."""

    pass


class GraphMigrationError(GraphError):
    """Schema migration failed or checksum mismatch."""

    pass


class GraphReadError(GraphError):
    """A read operation failed."""

    pass
