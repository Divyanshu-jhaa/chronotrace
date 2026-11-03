// V001 — Uniqueness constraints for stable node identifiers
// Idempotent: IF NOT EXISTS ensures safe re-runs and concurrent init
// Neo4j 5 supports CREATE CONSTRAINT ... IF NOT EXISTS ... REQUIRE ... IS UNIQUE

CREATE CONSTRAINT repository_id_unique IF NOT EXISTS FOR (n:Repository) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT author_id_unique IF NOT EXISTS FOR (n:Author) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT commit_id_unique IF NOT EXISTS FOR (n:Commit) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT file_id_unique IF NOT EXISTS FOR (n:File) REQUIRE n.id IS UNIQUE;
