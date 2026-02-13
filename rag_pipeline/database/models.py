"""
SQLAlchemy models for RAG Pipeline (MySQL compatible).
"""

import uuid
import hashlib
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, Boolean, Integer, CHAR, BigInteger, LargeBinary
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.types import TypeDecorator

Base = declarative_base()


class GUID(TypeDecorator):
    """Platform-independent GUID type for MySQL.
    Uses CHAR(36) to store UUID as a string.
    """
    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if isinstance(value, uuid.UUID):
                return str(value)
            return str(uuid.UUID(value))
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return uuid.UUID(value)
        return value


class DocumentIngestionState(Base):
    """
    Document ingestion state model - tracks document processing state.
    Maps to existing document_ingestion_state table.
    """
    __tablename__ = "document_ingestion_state"

    # Primary key - auto increment bigint
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Document identifier (unique)
    # For SharePoint pages: use SharePoint ID
    # For uploaded docs/external URLs: generate unique ID based on title/URL
    document_id = Column(String(255), unique=True, nullable=False, index=True)

    # Content hash for change detection - stored as binary(32)
    content_hash = Column(LargeBinary(32), nullable=False, index=True)

    # Timestamps
    last_processed_at = Column(DateTime(), nullable=True, index=True)
    last_content_update_at = Column(DateTime(), nullable=True)

    # Document metadata
    file_name = Column(String(512), nullable=True)
    url = Column(Text, nullable=True)

    def __repr__(self):
        return f"<DocumentIngestionState(id={self.id}, document_id='{self.document_id}', file_name='{self.file_name}')>"

    @staticmethod
    def compute_content_hash(content: str) -> bytes:
        """Compute SHA-256 hash of document content as binary."""
        if not content:
            return None
        return hashlib.sha256(content.encode()).digest()

    @staticmethod
    def compute_content_hash_hex(content: str) -> str:
        """Compute SHA-256 hash of document content as hex string."""
        if not content:
            return None
        return hashlib.sha256(content.encode()).hexdigest()

    def update_content_hash(self, content: str) -> bool:
        """Update content hash and last_content_update_at timestamp if content changed."""
        new_hash = self.compute_content_hash(content)
        if new_hash != self.content_hash:
            self.content_hash = new_hash
            self.last_content_update_at = datetime.now(timezone.utc)
            return True
        return False

    @staticmethod
    def generate_document_id(title: str, url: str = None, content: str = None) -> str:
        """
        Generate a unique document ID based on title and optional URL/content.

        For SharePoint pages, use the SharePoint ID directly.
        For uploaded documents and external URLs, generate based on title + URL.
        """
        data = title or ""
        if url:
            data += url
        if content:
            data += content[:100]  # Use first 100 chars of content

        # Generate UUID based on hash
        hash_bytes = hashlib.sha256(data.encode()).digest()[:16]
        return str(uuid.UUID(bytes=hash_bytes))


# Keep User model for future use (will be created when needed)
class User(Base):
    """
    User model for authentication and tracking document ownership.
    Similar to Django's auth_user table.
    """
    __tablename__ = "users"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    username = Column(String(150), unique=True, nullable=False, index=True)
    email = Column(String(254), unique=True, nullable=True, index=True)
    password_hash = Column(String(128), nullable=True)  # For future auth implementation
    first_name = Column(String(150), nullable=True)
    last_name = Column(String(150), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_staff = Column(Boolean, default=False, nullable=False)
    is_superuser = Column(Boolean, default=False, nullable=False)
    date_joined = Column(DateTime(), default=lambda: datetime.now(timezone.utc), nullable=False)
    last_login = Column(DateTime(), nullable=True)

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}')>"



