"""
Migration 002: Add rag_vector_ids column for multi-vector tracking.

This migration:
- Adds rag_vector_ids TEXT column to document_ingestion_state
- Stores JSON array of all section vector IDs per document
- Enables full cleanup of old vectors on re-ingestion
- Is idempotent (safe to re-run)

Usage:
    python -m rag_pipeline.database.migrations.002_add_vector_ids_array
"""

import sys
from sqlalchemy import text
from rag_pipeline.database.connection import engine
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


def run_migration():
    """Run the migration to add rag_vector_ids column."""
    if engine is None:
        raise RuntimeError("Database engine not initialized. Check database configuration.")

    logger.info("Starting migration 002: Add rag_vector_ids column")

    with engine.connect() as conn:
        try:
            # Check if column already exists
            check_query = text("""
                SELECT COUNT(*) as count
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'document_ingestion_state'
                  AND COLUMN_NAME = 'rag_vector_ids'
            """)
            result = conn.execute(check_query)
            exists = result.fetchone()[0] > 0

            if exists:
                logger.info("rag_vector_ids column already exists. Skipping.")
            else:
                logger.info("Adding rag_vector_ids column to document_ingestion_state...")

                conn.execute(text(
                    "ALTER TABLE document_ingestion_state ADD COLUMN rag_vector_ids TEXT NULL"
                ))
                conn.commit()

                logger.info("rag_vector_ids column added successfully.")

            logger.info("Migration 002 completed successfully!")

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
