"""Add source-revision checkpoints and durable SharePoint write-back retries.

Run before deploying the updated pipeline against an existing database:
    python -m rag_pipeline.database.migrations.004_add_ingestion_checkpoints
"""

from sqlalchemy import DateTime, Text, inspect, text

from rag_pipeline.database.connection import DB_SCHEMA, engine
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


def run_migration(bind=None):
    bind = bind if bind is not None else engine
    if bind is None:
        raise RuntimeError("Database engine not initialized")

    schema = DB_SCHEMA if bind.dialect.name == "postgresql" else None
    quote = bind.dialect.identifier_preparer.quote
    table = quote("document_ingestion_state")
    if schema:
        table = f"{quote(schema)}.{table}"
    columns = {
        "source_modified_at": DateTime(timezone=True),
        "source_attempt_modified_at": DateTime(timezone=True),
        "sharepoint_writeback_payload": Text(),
        "sharepoint_writeback_error": Text(),
    }
    with bind.begin() as conn:
        existing = {
            column["name"]
            for column in inspect(conn).get_columns("document_ingestion_state", schema=schema)
        }
        for name, column_type in columns.items():
            if name not in existing:
                sql_type = column_type.compile(dialect=bind.dialect)
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {quote(name)} {sql_type} NULL"))
                logger.info("Added ingestion checkpoint column: %s", name)
    logger.info("Migration 004 completed")


if __name__ == "__main__":
    run_migration()
