"""
Migration 001: Add RAG tracking fields and ingestion_locks table.

This migration:
- Adds RAG vector database tracking fields to document_ingestion_state
- Creates ingestion_locks table for distributed locking
- Adds indexes for query performance
- Is idempotent (safe to re-run)

Usage:
    python -m rag_pipeline.database.migrations.001_add_rag_fields
"""

import os
import sys
from sqlalchemy import text
from rag_pipeline.database.connection import engine
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


def run_migration():
    """Run the migration to add RAG tracking fields."""
    if engine is None:
        raise RuntimeError("Database engine not initialized. Check database configuration.")

    logger.info("Starting migration 001: Add RAG tracking fields")

    with engine.connect() as conn:
        try:
            # Check if columns already exist
            check_query = text("""
                SELECT COUNT(*) as count
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'document_ingestion_state'
                  AND COLUMN_NAME = 'rag_vector_id'
            """)
            result = conn.execute(check_query)
            exists = result.fetchone()[0] > 0

            if exists:
                logger.info("RAG fields already exist. Skipping column additions.")
            else:
                logger.info("Adding RAG tracking fields to document_ingestion_state...")

                # Add RAG tracking fields
                alter_statements = [
                    # Vector DB tracking
                    "ALTER TABLE document_ingestion_state ADD COLUMN rag_vector_id VARCHAR(255) NULL",
                    "ALTER TABLE document_ingestion_state ADD COLUMN rag_namespace VARCHAR(255) NULL",
                    "ALTER TABLE document_ingestion_state ADD COLUMN rag_last_ingested_at DATETIME NULL",
                    "ALTER TABLE document_ingestion_state ADD COLUMN rag_ingestion_status VARCHAR(50) DEFAULT 'pending' NOT NULL",
                    "ALTER TABLE document_ingestion_state ADD COLUMN rag_error_message TEXT NULL",
                    "ALTER TABLE document_ingestion_state ADD COLUMN rag_retry_count INT DEFAULT 0 NOT NULL",

                    # Section tracking
                    "ALTER TABLE document_ingestion_state ADD COLUMN sections_processed INT DEFAULT 0 NOT NULL",
                    "ALTER TABLE document_ingestion_state ADD COLUMN sections_total INT DEFAULT 0 NOT NULL",

                    # Deletion detection
                    "ALTER TABLE document_ingestion_state ADD COLUMN last_seen_at DATETIME NULL",
                ]

                for statement in alter_statements:
                    logger.info(f"Executing: {statement}")
                    conn.execute(text(statement))
                    conn.commit()

                logger.info("RAG fields added successfully.")

            # Check if indexes already exist
            check_index_query = text("""
                SELECT COUNT(*) as count
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'document_ingestion_state'
                  AND INDEX_NAME = 'idx_rag_vector_id'
            """)
            result = conn.execute(check_index_query)
            index_exists = result.fetchone()[0] > 0

            if index_exists:
                logger.info("Indexes already exist. Skipping index creation.")
            else:
                logger.info("Creating indexes for RAG fields...")

                # Create indexes
                index_statements = [
                    "CREATE INDEX idx_rag_vector_id ON document_ingestion_state(rag_vector_id)",
                    "CREATE INDEX idx_rag_last_ingested_at ON document_ingestion_state(rag_last_ingested_at)",
                    "CREATE INDEX idx_rag_ingestion_status ON document_ingestion_state(rag_ingestion_status)",
                    "CREATE INDEX idx_last_seen_at ON document_ingestion_state(last_seen_at)",
                ]

                for statement in index_statements:
                    logger.info(f"Executing: {statement}")
                    conn.execute(text(statement))
                    conn.commit()

                logger.info("Indexes created successfully.")

            # Check if ingestion_locks table exists
            check_table_query = text("""
                SELECT COUNT(*) as count
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'ingestion_locks'
            """)
            result = conn.execute(check_table_query)
            table_exists = result.fetchone()[0] > 0

            if table_exists:
                logger.info("ingestion_locks table already exists. Skipping table creation.")
            else:
                logger.info("Creating ingestion_locks table...")

                create_table_sql = text("""
                    CREATE TABLE ingestion_locks (
                        lock_key VARCHAR(255) PRIMARY KEY,
                        acquired_at DATETIME NOT NULL,
                        acquired_by VARCHAR(255) NOT NULL,
                        expires_at DATETIME NOT NULL,
                        INDEX idx_expires_at (expires_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)

                conn.execute(create_table_sql)
                conn.commit()

                logger.info("ingestion_locks table created successfully.")

            logger.info("Migration 001 completed successfully!")

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
