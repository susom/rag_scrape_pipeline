-- ============================================================================
-- RExI pipeline — DB readiness for the GKE ingestion pod
-- Run in Cloud SQL Studio against database `rexi_db` as `rexi_owner`.
-- Idempotent: safe to re-run.
-- ============================================================================
SET ROLE rexi_owner;

-- 1) Distributed-lock table (mirrors rag_pipeline.database.models.IngestionLock).
--    Timestamps are timestamptz because the pipeline writes timezone-aware UTC.
CREATE TABLE IF NOT EXISTS rexi.ingestion_locks (
    lock_key    varchar(255) PRIMARY KEY,
    acquired_at timestamptz  NOT NULL,
    acquired_by varchar(255) NOT NULL,
    expires_at  timestamptz  NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ingestion_locks_expires_at
    ON rexi.ingestion_locks (expires_at);

-- 2) Let the application role use the schema + tables + sequences.
--    (rag_chunks and document_ingestion_state were already granted to rexi_app;
--     these statements are idempotent and also cover the new lock table.)
GRANT USAGE ON SCHEMA rexi TO rexi_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON rexi.ingestion_locks TO rexi_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rexi TO rexi_app;

-- 3) Grant the GKE pod's IAM DB user DIRECTLY on all three tables + sequences.
--    (We grant object privileges instead of role membership: `GRANT rexi_app TO
--    <iam user>` needs ADMIN on rexi_app, which rexi_owner does not have. Direct
--    grants are within rexi_owner's power as the table owner, and don't depend
--    on the IAM role having INHERIT.)
GRANT USAGE ON SCHEMA rexi TO "gke-rexi-sa@som-rit-phi-rexi-dev.iam";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rexi
    TO "gke-rexi-sa@som-rit-phi-rexi-dev.iam";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rexi
    TO "gke-rexi-sa@som-rit-phi-rexi-dev.iam";

-- 4) Sanity check — should list rag_chunks, document_ingestion_state, ingestion_locks.
SELECT tablename FROM pg_tables WHERE schemaname = 'rexi' ORDER BY tablename;

-- ----------------------------------------------------------------------------
-- ALTERNATIVE (only if you prefer one role to manage): connect as the `postgres`
-- superuser and run the role-membership grant once, instead of step 3:
--     GRANT rexi_app TO "gke-rexi-sa@som-rit-phi-rexi-dev.iam";
-- ----------------------------------------------------------------------------
