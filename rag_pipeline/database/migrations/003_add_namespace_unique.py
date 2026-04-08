"""
Migration 003: Make document_id unique per namespace.

This migration:
- Backfills rag_namespace to "default" when NULL
- Drops the old unique index on document_id
- Adds a composite unique index on (document_id, rag_namespace)
- Is idempotent (safe to re-run)

Usage:
    python -m rag_pipeline.database.migrations.003_add_namespace_unique
"""

import sys
from sqlalchemy import text
from rag_pipeline.database.connection import engine
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


def run_migration():
    """Run the migration to make document_id unique per namespace."""
    if engine is None:
        raise RuntimeError("Database engine not initialized. Check database configuration.")

    logger.info("Starting migration 003: document_id unique per namespace")

    with engine.connect() as conn:
        try:
            # Backfill NULL namespaces so uniqueness works for the default namespace
            conn.execute(text(
                "UPDATE document_ingestion_state "
                "SET rag_namespace = 'default' "
                "WHERE rag_namespace IS NULL"
            ))
            conn.commit()

            # Check if composite unique index already exists
            check_index_query = text("""
                SELECT COUNT(*) as count
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'document_ingestion_state'
                  AND INDEX_NAME = 'uq_document_namespace'
            """)
            result = conn.execute(check_index_query)
            index_exists = result.fetchone()[0] > 0

            if index_exists:
                logger.info("Composite unique index already exists. Skipping.")
                logger.info("Migration 003 completed successfully!")
                return

            # Drop any existing unique index on document_id
            index_query = text("""
                SELECT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'document_ingestion_state'
                  AND COLUMN_NAME = 'document_id'
                  AND NON_UNIQUE = 0
            """)
            index_rows = conn.execute(index_query).fetchall()
            for (index_name,) in index_rows:
                if index_name and index_name != "PRIMARY":
                    logger.info(f"Dropping unique index: {index_name}")
                    conn.execute(text(f"DROP INDEX `{index_name}` ON document_ingestion_state"))
            conn.commit()

            # Create composite unique index
            logger.info("Creating composite unique index uq_document_namespace...")
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_document_namespace "
                "ON document_ingestion_state(document_id, rag_namespace)"
            ))
            conn.commit()

            logger.info("Migration 003 completed successfully!")

        except Exception as e:
            logger.error(f"Migration failed: {e}")
            conn.rollback()
            raise


if __name__ == "__main__":
    try:
        run_migration()
        sys.exit(0)
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)
