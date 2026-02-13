"""
Database connection module for RAG Pipeline.
Supports Cloud SQL via Unix socket or direct connection (MySQL).
"""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


def get_database_url() -> str:
    """
    Build database URL from environment variables.

    Supports:
    - Cloud SQL via Unix socket (for Cloud Run / local with cloud-sql-proxy)
    - Direct MySQL connection

    Environment variables:
    - DB_USER: Database username
    - DB_PASSWORD: Database password
    - DB_NAME: Database name (default: document_ingestion_state)
    - DB_HOST: Database host (for direct connection)
    - DB_PORT: Database port (default: 3306 for MySQL)
    - CLOUD_SQL_CONNECTION_NAME: Cloud SQL instance connection name
    - DB_SOCKET_DIR: Directory for Unix socket (default: /socket)
    """
    db_user = os.getenv("DB_USER", "root")
    db_password = os.getenv("DB_PASSWORD", "")
    db_name = os.getenv("DB_NAME", "document_ingestion_state")
    db_host = os.getenv("DB_HOST", "")
    db_port = os.getenv("DB_PORT", "3306")
    cloud_sql_connection_name = os.getenv("CLOUD_SQL_CONNECTION_NAME", "som-rit-phi-redcap-prod:us-west1:redcap-rag")
    socket_dir = os.getenv("DB_SOCKET_DIR", "/socket")

    # Check if Unix socket exists (Cloud SQL Proxy)
    socket_path = f"{socket_dir}/{cloud_sql_connection_name}"

    if os.path.exists(socket_path):
        # Use Unix socket connection (Cloud SQL Proxy)
        # For MySQL, the socket file is the full path
        database_url = (
            f"mysql+pymysql://{db_user}:{db_password}@/{db_name}"
            f"?unix_socket={socket_path}"
        )
        logger.info(f"Using Cloud SQL MySQL socket connection: {socket_path}")
    elif os.path.exists(socket_dir):
        # Socket directory exists but socket file not yet created - still try
        database_url = (
            f"mysql+pymysql://{db_user}:{db_password}@/{db_name}"
            f"?unix_socket={socket_path}"
        )
        logger.info(f"Using Cloud SQL MySQL socket connection (pending): {socket_path}")
    elif db_host:
        # Use direct TCP connection
        database_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
        logger.info(f"Using direct MySQL connection: {db_host}:{db_port}/{db_name}")
    else:
        # Fallback to localhost for development
        database_url = f"mysql+pymysql://{db_user}:{db_password}@localhost:{db_port}/{db_name}"
        logger.info(f"Using localhost MySQL connection: localhost:{db_port}/{db_name}")

    return database_url


# Create engine
try:
    engine = create_engine(
        get_database_url(),
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,  # Verify connections before use
    )
except Exception as e:
    logger.warning(f"Could not create database engine: {e}")
    engine = None

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None


def get_db():
    """
    Dependency for FastAPI endpoints to get a database session.
    Usage: db: Session = Depends(get_db)
    """
    if SessionLocal is None:
        raise RuntimeError("Database not configured")

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection() -> dict:
    """
    Check database connection status.

    Returns:
        dict with connection status and details
    """
    if engine is None:
        return {
            "connected": False,
            "error": "Database engine not initialized",
            "database": None,
            "tables": []
        }

    try:
        with engine.connect() as conn:
            # Test query (MySQL compatible)
            result = conn.execute(text("SELECT DATABASE(), USER(), VERSION()"))
            row = result.fetchone()

            # Get list of tables
            tables = list_tables()

            return {
                "connected": True,
                "database": row[0],
                "user": row[1],
                "version": row[2],
                "tables": tables,
                "error": None
            }
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return {
            "connected": False,
            "error": str(e),
            "database": None,
            "tables": []
        }


def list_tables() -> list:
    """
    List all tables in the current database.

    Returns:
        List of table names
    """
    if engine is None:
        return []

    try:
        with engine.connect() as conn:
            result = conn.execute(text("SHOW TABLES"))
            tables = [row[0] for row in result.fetchall()]
            return tables
    except Exception as e:
        logger.error(f"Failed to list tables: {e}")
        return []


def init_db():
    """
    Initialize database by creating all tables.
    Call this on application startup.
    """
    if engine is None:
        logger.warning("Cannot initialize database: engine not configured")
        return False

    try:
        from .models import Base
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")
        return False


