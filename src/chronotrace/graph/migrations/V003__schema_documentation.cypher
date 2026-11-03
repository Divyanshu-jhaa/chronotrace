// V003 — Schema documentation and auxiliary constraints
// This migration is intentionally lightweight and idempotent.
// It ensures the SchemaMigration tracking infrastructure exists and
// documents relationship types.  No additional constraints are required
// beyond V001/V002 for iteration 2, but we record schema intent here
// and create a documentation node for discoverability.

// Ensure the SchemaMigration label/constraint exists for version tracking
// (The runner creates SchemaMigration nodes via MERGE; this constraint
// guarantees version uniqueness at the DB level.)
CREATE CONSTRAINT schema_migration_version_unique IF NOT EXISTS FOR (n:SchemaMigration) REQUIRE n.version IS UNIQUE;

// Optional: existence constraints for future iterations (commented — Neo4j 5
// supports REQUIRE n.prop IS NOT NULL, but we keep these as documentation
// until properties are proven required in ingestion).
// Example (uncomment when enforced):
// CREATE CONSTRAINT commit_hash_not_null IF NOT EXISTS FOR (n:Commit) REQUIRE n.hash IS NOT NULL;
// CREATE CONSTRAINT file_path_not_null IF NOT EXISTS FOR (n:File) REQUIRE n.path IS NOT NULL;

// Create a singleton documentation node describing relationship types
// and co-change semantics (directional confidence, observed association).
// This node is merged idempotently and can be queried for schema docs.
MERGE (d:SchemaDocumentation {id: 'graph-schema-v003'})
ON CREATE SET
  d.version = 'V003',
  d.description = 'Initial graph schema: Repository, Author, Commit, File nodes and '
    + 'CONTAINS, BELONGS_TO, AUTHORED, MODIFIED, REVERTS, IMPORTS, CO_CHANGED_WITH relationships. '
    + 'Co-change confidence is directional (A->B may differ from B->A) and represents '
    + 'observed co-occurrence, not proven causation or dependency.',
  d.relationships = ['CONTAINS', 'BELONGS_TO', 'AUTHORED', 'MODIFIED', 'REVERTS', 'IMPORTS', 'CO_CHANGED_WITH'],
  d.created_at = datetime()
ON MATCH SET
  d.version = 'V003';
