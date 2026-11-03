// V002 — Indexes for frequently queried fields
// Idempotent: IF NOT EXISTS ensures safe re-runs
// Supports ingestion and analytics query patterns

CREATE INDEX commit_hash_index IF NOT EXISTS FOR (n:Commit) ON (n.hash);
CREATE INDEX file_path_index IF NOT EXISTS FOR (n:File) ON (n.path);
CREATE INDEX file_is_test_index IF NOT EXISTS FOR (n:File) ON (n.is_test);
CREATE INDEX commit_date_index IF NOT EXISTS FOR (n:Commit) ON (n.date);
CREATE INDEX repository_name_index IF NOT EXISTS FOR (n:Repository) ON (n.name);

// Additional helpful indexes (repository_id scoping) — not required but useful
CREATE INDEX commit_repository_id_index IF NOT EXISTS FOR (n:Commit) ON (n.repository_id);
CREATE INDEX file_repository_id_index IF NOT EXISTS FOR (n:File) ON (n.repository_id);
CREATE INDEX author_repository_id_index IF NOT EXISTS FOR (n:Author) ON (n.repository_id);
