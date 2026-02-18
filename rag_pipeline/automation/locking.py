"""
Distributed locking mechanism for preventing concurrent ingestion runs.

Uses database-backed advisory locks to ensure only one ingestion process
runs at a time across multiple Cloud Run instances.
"""

import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from rag_pipeline.database.models import IngestionLock
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


class LockAlreadyHeld(Exception):
    """Raised when attempting to acquire a lock that is already held."""
    pass


class DistributedLock:
    """
    Context manager for distributed locking using database records.

    Prevents concurrent execution of automated ingestion across multiple
    Cloud Run instances. Automatically cleans up stale locks.

    Usage:
        with DistributedLock("automated_ingestion", timeout_minutes=60):
            # Protected code - only one process can execute this at a time
            run_ingestion()

    Raises:
        LockAlreadyHeld: If lock is already held by another process
    """

    def __init__(
        self,
        lock_key: str,
        db_session: Session,
        timeout_minutes: int = 60,
    ):
        """
        Initialize distributed lock.

        Args:
            lock_key: Unique identifier for this lock (e.g., "automated_ingestion")
            db_session: SQLAlchemy database session
            timeout_minutes: Lock timeout in minutes (for stale lock cleanup)
        """
        self.lock_key = lock_key
        self.db_session = db_session
        self.timeout_minutes = timeout_minutes
        self.acquired = False

        # Generate unique identifier for this process
        hostname = socket.gethostname()
        pid = os.getpid()
        self.acquired_by = f"{hostname}:{pid}"

    def __enter__(self):
        """Acquire the lock."""
        self._acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Release the lock."""
        self._release()
        return False

    def _clean_stale_locks(self):
        """Remove locks that have expired."""
        try:
            now = datetime.now(timezone.utc)
            deleted = self.db_session.query(IngestionLock).filter(
                IngestionLock.expires_at < now
            ).delete()

            if deleted > 0:
                self.db_session.commit()
                logger.info(f"Cleaned up {deleted} stale lock(s)")

        except Exception as e:
            logger.warning(f"Failed to clean stale locks: {e}")
            self.db_session.rollback()

    def _acquire(self):
        """
        Acquire the lock.

        Raises:
            LockAlreadyHeld: If lock is already held by another active process
        """
        # Clean up stale locks first
        self._clean_stale_locks()

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=self.timeout_minutes)

        try:
            # Attempt to insert lock record
            lock = IngestionLock(
                lock_key=self.lock_key,
                acquired_at=now,
                acquired_by=self.acquired_by,
                expires_at=expires_at,
            )

            self.db_session.add(lock)
            self.db_session.commit()

            self.acquired = True
            logger.info(f"Lock '{self.lock_key}' acquired by {self.acquired_by}, expires at {expires_at}")

        except IntegrityError:
            # Lock already exists - check if it's active or stale
            self.db_session.rollback()

            existing_lock = self.db_session.query(IngestionLock).filter(
                IngestionLock.lock_key == self.lock_key
            ).first()

            if existing_lock:
                if existing_lock.expires_at < now:
                    # Stale lock - force cleanup and retry
                    logger.warning(
                        f"Found stale lock from {existing_lock.acquired_by}, "
                        f"expired at {existing_lock.expires_at}. Cleaning up..."
                    )
                    self.db_session.delete(existing_lock)
                    self.db_session.commit()

                    # Retry acquisition
                    self._acquire()
                else:
                    # Active lock held by another process
                    raise LockAlreadyHeld(
                        f"Lock '{self.lock_key}' is already held by {existing_lock.acquired_by}, "
                        f"acquired at {existing_lock.acquired_at}, expires at {existing_lock.expires_at}"
                    )
            else:
                # Race condition - lock was deleted between check and insert
                logger.warning("Lock disappeared during acquisition, retrying...")
                self._acquire()

        except Exception as e:
            self.db_session.rollback()
            logger.error(f"Failed to acquire lock: {e}")
            raise

    def _release(self):
        """Release the lock if it was successfully acquired."""
        if not self.acquired:
            return

        try:
            deleted = self.db_session.query(IngestionLock).filter(
                IngestionLock.lock_key == self.lock_key,
                IngestionLock.acquired_by == self.acquired_by,
            ).delete()

            self.db_session.commit()

            if deleted > 0:
                logger.info(f"Lock '{self.lock_key}' released by {self.acquired_by}")
            else:
                logger.warning(
                    f"Lock '{self.lock_key}' not found during release - may have been cleaned up as stale"
                )

            self.acquired = False

        except Exception as e:
            logger.error(f"Failed to release lock: {e}")
            self.db_session.rollback()
            raise

    def extend_lock(self, additional_minutes: int = 30):
        """
        Extend the lock expiration time.

        Useful for long-running operations that may exceed the initial timeout.

        Args:
            additional_minutes: Minutes to add to expiration time
        """
        if not self.acquired:
            raise RuntimeError("Cannot extend lock that is not held")

        try:
            lock = self.db_session.query(IngestionLock).filter(
                IngestionLock.lock_key == self.lock_key,
                IngestionLock.acquired_by == self.acquired_by,
            ).first()

            if not lock:
                raise RuntimeError("Lock not found - may have been cleaned up")

            new_expires_at = lock.expires_at + timedelta(minutes=additional_minutes)
            lock.expires_at = new_expires_at

            self.db_session.commit()

            logger.info(f"Lock '{self.lock_key}' extended to {new_expires_at}")

        except Exception as e:
            logger.error(f"Failed to extend lock: {e}")
            self.db_session.rollback()
            raise
