"""
Database connection module for RAG Pipeline.

Supports two engines, selected by DB_ENGINE:
  - "mysql" (default)      -> SOM/REDCap tracking DB (Cloud SQL MySQL, redcap-rag)
  - "postgresql"/"postgres" -> RExI tracking DB (Cloud SQL Postgres, rexi-dev)

Each engine supports either a Cloud SQL proxy Unix socket (Cloud Run sidecar)
or a direct TCP connection (local dev / proxy on a TCP port).

Postgres-specific:
  - DB_SCHEMA: search_path schema for tracking tables (e.g. "rpp"). Set on every
    connection. MySQL ignores this.
"""

import os
import ssl
from urllib.parse import quote_plus
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import sessionmaker
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

DB_SCHEMA = os.getenv("DB_SCHEMA", "").strip() or None

# When the runtime user has no DDL rights (e.g. the GKE pod's gke-rexi-sa, which
# only holds CRUD on a pre-created schema), skip all CREATE statements in init_db
# and just verify connectivity. Set DB_SKIP_INIT_DDL=true for such deployments.
DB_SKIP_INIT_DDL = os.getenv("DB_SKIP_INIT_DDL", "").strip().lower() in ("1", "true", "yes", "on")

# Cloud SQL IAM database authentication (Postgres). When true, DB_PASSWORD is
# ignored; instead each new connection uses a short-lived OAuth2 access token
# (scope sqlservice.login) as the password over TLS — mirroring how the RExI
# Java app (GooglePool) connects to rexi.db.internal as the gke-rexi-sa IAM user.
DB_IAM_AUTH = os.getenv("DB_IAM_AUTH", "").strip().lower() in ("1", "true", "yes", "on")

_IAM_DB_SCOPE = "https://www.googleapis.com/auth/sqlservice.login"


def _fetch_iam_db_token() -> str:
    """Mint a fresh GCP access token to use as the Cloud SQL IAM DB password."""
    import google.auth
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=[_IAM_DB_SCOPE])
    creds.refresh(Request())
    return creds.token


def _db_engine_kind() -> str:
    """Return the configured engine family: 'postgresql' or 'mysql' (default)."""
    val = os.getenv("DB_ENGINE", "mysql").strip().lower()
    if val in ("postgres", "postgresql", "pg"):
        return "postgresql"
    return "mysql"


def get_engine_config() -> tuple[str, dict]:
    """
    Build (database_url, connect_args) for the configured DB engine.

    Common environment variables:
    - DB_ENGINE: "mysql" (default) or "postgresql"
    - DB_USER / DB_PASSWORD / DB_NAME
    - DB_HOST / DB_PORT (TCP connection)
    - CLOUD_SQL_CONNECTION_NAME: Cloud SQL instance connection name
    - DB_SOCKET_DIR: Directory for Unix socket (default: /socket)
    """
    kind = _db_engine_kind()
    db_user = os.getenv("DB_USER", "root")
    db_password = os.getenv("DB_PASSWORD", "")
    db_host = os.getenv("DB_HOST", "")
    socket_dir = os.getenv("DB_SOCKET_DIR", "/socket")
    cloud_sql_connection_name = os.getenv(
        "CLOUD_SQL_CONNECTION_NAME", "som-rit-phi-redcap-prod:us-west1:redcap-rag"
    )

    if kind == "postgresql":
        db_name = os.getenv("DB_NAME", "rexi_db")
        db_port = os.getenv("DB_PORT", "5432")
        # Cloud SQL Postgres proxy socket file: <dir>/<conn>/.s.PGSQL.5432
        pg_socket = f"{socket_dir}/{cloud_sql_connection_name}/.s.PGSQL.5432"

        if not db_host and (os.path.exists(pg_socket) or os.path.exists(socket_dir)):
            # Unix socket via Cloud SQL proxy sidecar
            url = f"postgresql+pg8000://{db_user}:{db_password}@/{db_name}"
            logger.info(f"Using Cloud SQL Postgres socket connection: {pg_socket}")
            return url, {"unix_sock": pg_socket}

        host = db_host or "localhost"
        safe_user = quote_plus(db_user)
        safe_pw = quote_plus(db_password)
        url = f"postgresql+pg8000://{safe_user}:{safe_pw}@{host}:{db_port}/{db_name}"
        logger.info(f"Using direct Postgres connection: {host}:{db_port}/{db_name}")
        return url, {}

    # ---- MySQL (default / SOM) — behavior preserved ----
    db_name = os.getenv("DB_NAME", "document_ingestion_state")
    db_port = os.getenv("DB_PORT", "3306")
    socket_path = f"{socket_dir}/{cloud_sql_connection_name}"

    if os.path.exists(socket_path):
        url = f"mysql+pymysql://{db_user}:{db_password}@/{db_name}?unix_socket={socket_path}"
        logger.info(f"Using Cloud SQL MySQL socket connection: {socket_path}")
    elif os.path.exists(socket_dir):
        url = f"mysql+pymysql://{db_user}:{db_password}@/{db_name}?unix_socket={socket_path}"
        logger.info(f"Using Cloud SQL MySQL socket connection (pending): {socket_path}")
    elif db_host:
        url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
        logger.info(f"Using direct MySQL connection: {db_host}:{db_port}/{db_name}")
    else:
        url = f"mysql+pymysql://{db_user}:{db_password}@localhost:{db_port}/{db_name}"
        logger.info(f"Using localhost MySQL connection: localhost:{db_port}/{db_name}")

    return url, {}


def get_database_url() -> str:
    """Backwards-compatible accessor returning only the URL string."""
    return get_engine_config()[0]


# Create engine
try:
    _url, _connect_args = get_engine_config()
    engine = create_engine(
        _url,
        connect_args=_connect_args,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,  # Verify connections before use
    )

    # On Postgres, route the (schema-agnostic) models into the tracking schema
    # (e.g. "rpp"). schema_translate_map makes DDL/DML explicitly qualify table
    # names with the schema — deterministic regardless of connection/search_path
    # timing. We ALSO pin search_path so raw SQL and ad-hoc queries resolve there.
    # MySQL ignores all of this.
    if engine.dialect.name == "postgresql" and DB_SCHEMA:
        @event.listens_for(engine, "connect")
        def _set_search_path(dbapi_conn, _conn_record):
            cur = dbapi_conn.cursor()
            # Include public so extension types (e.g. pgvector's `vector`) resolve
            # regardless of which schema the extension was installed into; tables
            # still resolve to DB_SCHEMA first.
            cur.execute(f'SET search_path TO "{DB_SCHEMA}", public')
            cur.close()

        engine = engine.execution_options(schema_translate_map={None: DB_SCHEMA})

    # Cloud SQL IAM auth: inject a fresh access token as the password (and require
    # TLS) on every new physical connection, so tokens never go stale in the pool.
    if engine.dialect.name == "postgresql" and DB_IAM_AUTH:
        _iam_ssl_ctx = ssl.create_default_context()
        _iam_ssl_ctx.check_hostname = False
        _iam_ssl_ctx.verify_mode = ssl.CERT_NONE

        @event.listens_for(engine, "do_connect")
        def _provide_iam_token(_dialect, _conn_rec, _cargs, cparams):
            cparams["password"] = _fetch_iam_db_token()
            cparams["ssl_context"] = _iam_ssl_ctx

        logger.info("Cloud SQL IAM database authentication enabled (token-as-password + TLS)")

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
            if engine.dialect.name == "postgresql":
                result = conn.execute(
                    text("SELECT current_database(), current_user, version()")
                )
            else:
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
        from sqlalchemy import inspect as _sa_inspect
        inspector = _sa_inspect(engine)
        schema = DB_SCHEMA if engine.dialect.name == "postgresql" else None
        return inspector.get_table_names(schema=schema)
    except Exception as e:
        logger.error(f"Failed to list tables: {e}")
        return []


def init_db():
    """
    Initialize database by creating all tables.
    Call this on application startup.

    On Postgres, if DB_SCHEMA is set, the schema is created first so the
    (schema-agnostic) models land inside it (search_path is pinned per-connection).
    Requires CREATE privilege on the target schema/database.
    """
    if engine is None:
        logger.warning("Cannot initialize database: engine not configured")
        return False

    if DB_SKIP_INIT_DDL:
        # Pod has no DDL rights; tables are pre-created. Verify connectivity only.
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("DB_SKIP_INIT_DDL set — skipping schema/table creation (connectivity OK)")
            return True
        except Exception as e:
            logger.error(f"Database connectivity check failed: {e}")
            return False

    try:
        if engine.dialect.name == "postgresql" and DB_SCHEMA:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))
            logger.info(f"Ensured Postgres schema exists: {DB_SCHEMA}")

        from .models import Base
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")
        return False


