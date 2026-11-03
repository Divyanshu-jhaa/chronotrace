"""Shared Neo4j driver lifecycle with single-driver reuse."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import GraphConnectionError, GraphNotAvailable


def _sanitize_error_message(msg: str) -> str:
    """Remove credential-like substrings from error messages."""
    # Defensive: never include password values; strip obvious patterns
    # We don't have credentials here, so just return raw but ensure no
    # accidental leak from underlying exception that might echo auth tuple.
    # Remove any occurrence of password-like tokens if present.
    # Keep it simple: truncate overly long messages and filter common secrets.
    lower = msg.lower()
    for token in ["password", "passwd", "secret", "token"]:
        if token in lower:
            # If message contains auth details, replace with placeholder
            # Don't try to parse, just indicate redacted
            return "[Graph connection error — details redacted for security]"
    return msg


def _resolve_credentials(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Resolve NEO4J username/password from env without logging.

    Supports NEO4J_USERNAME / NEO4J_PASSWORD, NEO4J_USER / NEO4J_PASS,
    and NEO4J_AUTH in the form 'user/password' or 'neo4j/pass'.
    Defaults to neo4j/password (docker-compose.yml) when no env is set,
    so local Docker works zero-config. Does not log values.
    """
    username = env.get("NEO4J_USERNAME") or env.get("NEO4J_USER")
    password = env.get("NEO4J_PASSWORD") or env.get("NEO4J_PASS")
    # NEO4J_AUTH fallback (e.g., "neo4j/password")
    auth_raw = env.get("NEO4J_AUTH")
    if auth_raw and (not username or not password):
        # NEO4J_AUTH can be "user/pass" or "none"
        if auth_raw.lower() != "none" and "/" in auth_raw:
            parts = auth_raw.split("/", 1)
            if not username:
                username = parts[0] if parts[0].lower() != "none" else None
            if not password:
                password = parts[1] if len(parts) > 1 and parts[1].lower() != "none" else None
    # Zero-config defaults for internal Docker (must match docker-compose.yml)
    if not username:
        username = "neo4j"
    if not password:
        password = "password"
    return username, password


class GraphDriver:
    """Application-wide Neo4j driver with single instance reuse.

    Consumes a validated config object (never re-parses YAML/env).
    Credentials are resolved from env mapping only, without logging.
    Lifecycle: create once, reuse for all sessions, verify, close idempotently.
    """

    def __init__(
        self,
        config: Any,
        env: Mapping[str, str] | None = None,
        *,
        uri: str | None = None,
        database: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        # Config is ChronoTraceConfig but typed as Any to avoid hard import cycle
        env_map: Mapping[str, str] = env if env is not None else {}

        # Prefer explicit overrides (for testing), else config
        if uri is not None:
            self._uri = uri
        elif hasattr(config, "graph") and hasattr(config.graph, "uri"):
            self._uri = config.graph.uri
        else:
            # Allow dict-like config
            self._uri = getattr(config, "uri", "bolt://localhost:7687")

        if database is not None:
            self._database = database
        elif hasattr(config, "graph") and hasattr(config.graph, "database"):
            self._database = config.graph.database
        else:
            self._database = getattr(config, "database", "neo4j")

        # Batch size validation: 1..10000
        raw_batch = None
        if hasattr(config, "graph") and hasattr(config.graph, "batch_size"):
            raw_batch = config.graph.batch_size
        elif hasattr(config, "batch_size"):
            raw_batch = config.batch_size
        if raw_batch is not None:
            try:
                b = int(raw_batch)
            except Exception as exc:
                from .errors import GraphValidationError

                raise GraphValidationError(
                    f"batch_size must be an integer, got {raw_batch!r}"
                ) from exc
            if b < 1 or b > 10000:
                from .errors import GraphValidationError

                raise GraphValidationError(f"batch_size must be between 1 and 10000, got {b}")
            self._batch_size: int = b
        else:
            self._batch_size = 500

        # Credentials — never log
        if username is not None or password is not None:
            self._username = username
            self._password = password
        else:
            u, p = _resolve_credentials(env_map)
            self._username = u
            self._password = p

        self._closed = False
        self._driver: Any = None

        # Lazily create driver but validate that neo4j is importable at init time
        self._create_driver()

    def _create_driver(self) -> None:
        if self._driver is not None:
            return
        try:
            from neo4j import GraphDatabase
        except Exception as e:
            raise GraphConnectionError(f"Neo4j driver not available: {e}") from e

        auth = None
        if self._username is not None or self._password is not None:
            auth = (self._username or "", self._password or "")

        try:
            # Single driver instance for the process — short timeout so
            # integration tests skip quickly when Neo4j is unavailable
            # (otherwise verify_connectivity hangs ~30s per live test)
            kwargs: dict[str, Any] = {"connection_timeout": 5}
            if auth is not None:
                self._driver = GraphDatabase.driver(self._uri, auth=auth, **kwargs)
            else:
                self._driver = GraphDatabase.driver(self._uri, **kwargs)
        except Exception as e:
            msg = _sanitize_error_message(str(e))
            raise GraphConnectionError(msg) from e

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def database(self) -> str:
        return self._database

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def is_closed(self) -> bool:
        return self._closed

    def verify_connectivity(self) -> None:
        """Verify connectivity to Neo4j.

        Raises GraphConnectionError / GraphNotAvailable on failure.
        Never leaks credentials or raw driver messages containing secrets.
        """
        if self._closed or self._driver is None:
            raise GraphNotAvailable("Graph driver is closed — cannot verify connectivity")
        try:
            self._driver.verify_connectivity()
            # Also verify we can open a session for the configured database
            # Use a lightweight query
            session = self._driver.session(database=self._database)
            try:
                session.run("RETURN 1 AS ok").consume()
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        except Exception as e:
            msg = _sanitize_error_message(str(e)) if str(e) else e.__class__.__name__
            # Preserve domain error types already
            from .errors import GraphConnectionError as GCE
            from .errors import GraphNotAvailable as GNA

            if isinstance(e, (GCE, GNA)):
                raise
            # Generic mapping: treat all connectivity failures as GraphConnectionError
            raise GraphConnectionError(f"Neo4j connectivity check failed: {msg}") from e

    def session(self, database: str | None = None) -> Any:
        """Return a Neo4j session for the configured (or overridden) database.

        Caller must use as context manager: ``with driver.session() as s:``.
        Sessions are short-lived; driver never creates per-query drivers.
        """
        if self._closed or self._driver is None:
            raise GraphNotAvailable("Graph driver is closed — cannot create session")
        db = database if database is not None else self._database
        try:
            return self._driver.session(database=db)
        except Exception as e:
            msg = _sanitize_error_message(str(e))
            raise GraphConnectionError(f"Failed to create session: {msg}") from e

    def close(self) -> None:
        """Idempotent close — releases connection pool."""
        if self._closed:
            return
        self._closed = True
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            finally:
                self._driver = None

    def __enter__(self) -> GraphDriver:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # Internal accessor for repositories that need raw driver for advanced use
    @property
    def _raw_driver(self) -> Any:
        if self._closed or self._driver is None:
            raise GraphNotAvailable("Graph driver is closed")
        return self._driver
