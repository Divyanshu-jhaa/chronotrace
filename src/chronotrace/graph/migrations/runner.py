"""Migration runner with idempotency and checksum tracking."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import GraphMigrationError

# Also provide SchemaManager alias for spec compatibility
__all__ = ["MigrationRunner", "SchemaManager", "Migration", "initialize_schema"]


@dataclass(frozen=True)
class Migration:
    version: str  # e.g., V001
    name: str  # e.g., V001__initial_constraints
    filename: str
    path: Path
    checksum: str
    content: str


_VERSION_RE = re.compile(r"^(V\d+)__.*\.cypher$")


def _compute_checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _discover_migrations(migrations_dir: Path) -> list[Migration]:
    if not migrations_dir.exists():
        raise GraphMigrationError(f"Migrations directory not found: {migrations_dir}")
    files = sorted(migrations_dir.glob("V*.cypher"))
    migrations: list[Migration] = []
    for p in files:
        m = _VERSION_RE.match(p.name)
        if not m:
            continue
        version = m.group(1)
        text = p.read_text(encoding="utf-8")
        checksum = _compute_checksum(text)
        migrations.append(
            Migration(
                version=version,
                name=p.stem,
                filename=p.name,
                path=p,
                checksum=checksum,
                content=text,
            )
        )
    # Sort by version lexicographically (V001 < V002 ...)
    migrations.sort(key=lambda x: x.version)
    return migrations


def _sanitize(msg: str) -> str:
    # Never include secrets; just return message
    if "password" in msg.lower() or "secret" in msg.lower():
        return "[Migration error — details redacted]"
    return msg


class MigrationRunner:
    """Discovers, validates, and applies versioned Cypher migrations.

    Tracking is done via :SchemaMigration nodes with properties
    ``version``, ``checksum``, ``applied_at``, ``filename``.
    The runner is idempotent: repeated runs skip applied versions,
    and checksum mismatches raise GraphMigrationError.
    """

    def __init__(self, driver: Any, migrations_dir: Path | None = None) -> None:
        self._driver = driver
        if migrations_dir is None:
            # Default to package migrations dir
            migrations_dir = Path(__file__).parent
        self._migrations_dir = Path(migrations_dir)

    def discover(self) -> list[Migration]:
        return _discover_migrations(self._migrations_dir)

    def _fetch_applied(self) -> dict[str, str]:
        """Return dict version -> checksum for already applied migrations."""
        query = "MATCH (m:SchemaMigration) RETURN m.version AS version, m.checksum AS checksum"
        try:
            with self._driver.session(database=self._driver.database) as session:
                result = session.execute_read(lambda tx: list(tx.run(query).data()))
                # Fallback if execute_read not needed: direct
        except Exception:
            # Fallback for drivers that don't support execute_read wrapper
            try:
                with self._driver.session(database=self._driver.database) as session:
                    records = session.run(query).data()
                    return {r["version"]: r["checksum"] for r in records}
            except Exception as e:
                raise GraphMigrationError(_sanitize(str(e))) from e

        # If the first path succeeded
        try:
            # result is list of dicts from the lambda
            if isinstance(result, list):
                return {r["version"]: r["checksum"] for r in result if r.get("version")}
            return {}
        except Exception as e:
            raise GraphMigrationError(_sanitize(str(e))) from e

    def _fetch_applied_fallback(self) -> dict[str, str]:
        query = "MATCH (m:SchemaMigration) RETURN m.version AS version, m.checksum AS checksum"
        try:
            with self._driver.session(database=self._driver.database) as session:
                # Use execute_read if available, else direct run
                try:
                    # Prefer execute_read
                    def _read(tx: Any) -> list[dict[str, Any]]:
                        return list(tx.run(query).data())

                    result = session.execute_read(_read)
                    return {r["version"]: r["checksum"] for r in result if r.get("version")}
                except AttributeError:
                    # No execute_read support (mock), fallback
                    records = session.run(query).data()
                    return {r["version"]: r["checksum"] for r in records}
        except Exception as e:
            # If SchemaMigration label doesn't exist yet, treat as empty
            msg = str(e).lower()
            if "not found" in msg or "no such" in msg:
                return {}
            raise GraphMigrationError(_sanitize(str(e))) from e

    def get_applied_versions(self) -> dict[str, str]:
        """Public accessor for applied migrations."""
        try:
            return self._fetch_applied_fallback()
        except GraphMigrationError:
            raise
        except Exception as e:
            raise GraphMigrationError(_sanitize(str(e))) from e

    def run(self, target_version: str | None = None) -> list[str]:
        """Apply pending migrations in order.

        Args:
            target_version: if provided, only apply up to and including this
                version (e.g., "V002").

        Returns:
            List of versions that were applied in this invocation.

        Raises:
            GraphMigrationError on checksum mismatch or execution failure.
        """
        migrations = self.discover()
        if target_version is not None:
            migrations = [m for m in migrations if m.version <= target_version]

        applied = self.get_applied_versions()

        # Validate checksums for already-applied migrations
        for m in migrations:
            if m.version in applied and applied[m.version] != m.checksum:
                raise GraphMigrationError(
                    f"Checksum mismatch for already-applied migration {m.version} "
                    f"({m.filename}): file was modified after application. "
                    f"Expected {applied[m.version][:8]}..., got {m.checksum[:8]}... "
                    f"Manual resolution required."
                )

        # Determine pending
        pending = [m for m in migrations if m.version not in applied]

        applied_now: list[str] = []
        for m in pending:
            self._apply_single(m)
            applied_now.append(m.version)
        return applied_now

    def _apply_single(self, migration: Migration) -> None:
        content = migration.content.strip()
        if not content:
            return

        # Split into individual statements — Neo4j driver requires exactly one
        # Cypher statement per query. Running multiple CREATE CONSTRAINT/INDEX
        # statements in a single tx.run fails with "Expected exactly one
        # statement". We therefore split on ';' and run each via autocommit
        # (session.run) so a failed statement does not poison a transaction.
        # Remove // comment lines first so semicolons inside comments (e.g.
        # "// via MERGE; this constraint") don't create spurious splits.
        cleaned_lines = [l for l in content.splitlines() if not l.strip().startswith("//")]
        cleaned = "\n".join(cleaned_lines)
        raw_parts = cleaned.split(";")
        statements: list[str] = []
        for part in raw_parts:
            stmt = part.strip()
            if not stmt:
                continue
            upper = stmt.upper()
            if any(kw in upper for kw in ("CREATE", "MERGE", "DROP", "INDEX", "CONSTRAINT")):
                statements.append(stmt)
            elif stmt:
                statements.append(stmt)
        if not statements:
            statements = [content]

        def _record(tx: Any) -> None:
            now = datetime.now(UTC).isoformat()
            tx.run(
                """
                MERGE (m:SchemaMigration {version: $version})
                ON CREATE SET m.checksum = $checksum,
                    m.filename = $filename, m.applied_at = $applied_at
                ON MATCH SET m.checksum = $checksum,
                    m.filename = $filename
                """,
                version=migration.version,
                checksum=migration.checksum,
                filename=migration.filename,
                applied_at=now,
            ).consume()

        try:
            with self._driver.session(database=self._driver.database) as session:
                # Execute each DDL statement as autocommit to avoid reusing a
                # failed transaction (once tx.run fails, the transaction is
                # marked failed and every subsequent tx.run raises TransactionError).
                for stmt in statements:
                    try:
                        session.run(stmt).consume()
                    except AttributeError:
                        # Mock driver without session.run — try execute_write
                        session.execute_write(lambda tx, s=stmt: tx.run(s).consume())  # type: ignore
                    except Exception:
                        # For mocks, tx.run may be MagicMock; re-raise real DDL errors
                        # Try fallback via execute_write before giving up
                        try:
                            session.execute_write(lambda tx, s=stmt: tx.run(s).consume())  # type: ignore
                        except Exception:
                            raise
                # Record migration in its own transaction
                try:
                    session.execute_write(_record)
                except AttributeError:
                    _record(session)  # type: ignore
        except GraphMigrationError:
            raise
        except Exception as e:
            raise GraphMigrationError(_sanitize(f"Failed to apply {migration.version}: {e}")) from e

    def verify_schema(self) -> dict[str, Any]:
        """Check constraints and indexes existence (for integration tests)."""
        try:
            with self._driver.session(database=self._driver.database) as session:
                try:
                    constraints = session.execute_read(
                        lambda tx: list(tx.run("SHOW CONSTRAINTS").data())
                    )
                except AttributeError:
                    constraints = list(session.run("SHOW CONSTRAINTS").data())
                try:
                    indexes = session.execute_read(
                        lambda tx: list(tx.run("SHOW INDEXES").data())
                    )
                except AttributeError:
                    indexes = list(session.run("SHOW INDEXES").data())
                return {"constraints": constraints, "indexes": indexes}
        except Exception as e:
            raise GraphMigrationError(_sanitize(str(e))) from e


# Alias
SchemaManager = MigrationRunner


def initialize_schema(driver: Any, migrations_dir: Path | None = None) -> list[str]:
    """Helper that invokes the runner to latest version."""
    runner = MigrationRunner(driver, migrations_dir=migrations_dir)
    return runner.run()
