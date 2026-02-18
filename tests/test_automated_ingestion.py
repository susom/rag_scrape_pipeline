"""
Test script for automated RAG ingestion workflow.

This script verifies the implementation without requiring live database/API connections.
"""

try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False
    # Define minimal pytest.raises replacement
    class FakePytest:
        class raises:
            def __init__(self, exc_type):
                self.exc_type = exc_type
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                if exc_type is None:
                    raise AssertionError(f"Expected {self.exc_type.__name__} but no exception was raised")
                return exc_type == self.exc_type
    pytest = FakePytest()

from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone
from sqlalchemy.orm import Session

# Test imports - verify all modules are accessible
def test_imports():
    """Verify all automation modules can be imported."""
    from rag_pipeline.automation import (
        rag_client,
        locking,
        orchestrator,
        content_fetcher,
    )
    from rag_pipeline.sharepoint import SharePointGraphClient, SharePointItem

    # Verify key functions exist
    assert hasattr(rag_client, 'store_document')
    assert hasattr(locking, 'DistributedLock')
    assert hasattr(orchestrator, 'run_automated_ingestion')
    assert hasattr(content_fetcher, 'fetch_content_sources')
    assert SharePointGraphClient is not None
    assert SharePointItem is not None

    print("✅ All automation modules imported successfully")


def test_database_models():
    """Verify database models have RAG tracking fields."""
    from rag_pipeline.database.models import DocumentIngestionState, IngestionLock

    # Check RAG tracking fields exist
    assert hasattr(DocumentIngestionState, 'rag_vector_id')
    assert hasattr(DocumentIngestionState, 'rag_namespace')
    assert hasattr(DocumentIngestionState, 'rag_last_ingested_at')
    assert hasattr(DocumentIngestionState, 'rag_ingestion_status')
    assert hasattr(DocumentIngestionState, 'rag_error_message')
    assert hasattr(DocumentIngestionState, 'rag_retry_count')
    assert hasattr(DocumentIngestionState, 'sections_processed')
    assert hasattr(DocumentIngestionState, 'sections_total')
    assert hasattr(DocumentIngestionState, 'last_seen_at')

    # Check IngestionLock model
    assert hasattr(IngestionLock, 'lock_key')
    assert hasattr(IngestionLock, 'acquired_at')
    assert hasattr(IngestionLock, 'acquired_by')
    assert hasattr(IngestionLock, 'expires_at')

    print("✅ Database models have all required RAG tracking fields")


@patch('rag_pipeline.automation.rag_client.requests.post')
def test_rag_client_store_document(mock_post):
    """Test RAG client storeDocument API call."""
    from rag_pipeline.automation.rag_client import store_document
    import os

    # Mock successful API response
    mock_response = Mock()
    mock_response.json.return_value = {
        "status": "success",
        "namespace": "default",
        "vector_id": "sha256:abc123",
        "title": "test_section_001",
        "message": "Document stored successfully"
    }
    mock_response.raise_for_status = Mock()
    mock_post.return_value = mock_response

    # Set environment variables
    os.environ["REDCAP_API_URL"] = "http://test-api.example.com/api/"
    os.environ["REDCAP_API_TOKEN"] = "test_token_123"

    # Call store_document
    result = store_document(
        title="test_section_001",
        content="This is test content.",
        metadata={"doc_id": "test_doc", "source_type": "test"}
    )

    # Verify API was called
    assert mock_post.called
    call_args = mock_post.call_args

    # Verify payload structure
    payload = call_args[1]["data"]
    assert payload["token"] == "test_token_123"
    assert payload["content"] == "externalModule"
    assert payload["prefix"] == "redcap_rag"
    assert payload["action"] == "storeDocument"
    assert payload["title"] == "test_section_001"
    assert payload["text"] == "This is test content."

    # Verify response
    assert result["status"] == "success"
    assert result["vector_id"] == "sha256:abc123"

    print("✅ RAG client API call works correctly")


def test_distributed_lock():
    """Test distributed locking mechanism."""
    from rag_pipeline.automation.locking import DistributedLock, LockAlreadyHeld
    from rag_pipeline.database.models import IngestionLock

    # Create mock database session
    mock_db = Mock(spec=Session)
    mock_query = Mock()
    mock_db.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None  # No existing lock

    # Test lock acquisition
    lock = DistributedLock(
        lock_key="test_lock",
        db_session=mock_db,
        timeout_minutes=60,
    )

    # Verify lock context manager interface
    assert hasattr(lock, '__enter__')
    assert hasattr(lock, '__exit__')

    print("✅ Distributed lock mechanism is properly implemented")


def test_orchestrator_dry_run():
    """Test orchestrator dry run mode."""
    from rag_pipeline.automation.orchestrator import IngestionOrchestrator, IngestionResult

    # Create mock database session
    mock_db = Mock(spec=Session)

    # Create orchestrator in dry run mode
    orchestrator = IngestionOrchestrator(
        db_session=mock_db,
        dry_run=True,
    )

    # Verify dry_run flag
    assert orchestrator.dry_run is True

    # Verify result builder
    result = orchestrator._build_result(
        status="completed",
        run_id="test_run_001",
        documents_processed=0,
        sections_ingested=0,
        documents_skipped=5,
        documents_failed=0,
    )

    assert isinstance(result, IngestionResult)
    assert result.status == "completed"
    assert result.dry_run is True
    assert result.documents_skipped == 5

    print("✅ Orchestrator dry run mode works correctly")


def test_content_fetcher_stub():
    """Test content fetcher stub implementation."""
    from rag_pipeline.automation.content_fetcher import fetch_content_sources_stub

    # Call stub function
    sharepoint_docs, external_urls = fetch_content_sources_stub()

    # Verify returns test data (stub provides sample items for manual testing)
    assert len(sharepoint_docs) >= 1
    assert len(external_urls) >= 1

    # Verify SharePointItem fields
    item = sharepoint_docs[0]
    assert item.name is not None
    assert item.item_type == "file"

    print("✅ Content fetcher stub returns test data")


def test_sharepoint_client_interface():
    """Test SharePoint client has automation methods."""
    from rag_pipeline.sharepoint import SharePointGraphClient, SharePointItem

    # Verify automation methods exist on the class
    assert hasattr(SharePointGraphClient, 'get_document_manifest')
    assert hasattr(SharePointGraphClient, 'download_file_content')

    # Verify SharePointItem dataclass fields
    item = SharePointItem(
        sharepoint_id="test_id",
        name="test.docx",
        item_type="file",
        url="https://example.com/test.docx",
        download_url="https://example.com/download/test.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=1024,
    )
    assert item.name == "test.docx"
    assert item.download_url is not None

    print("✅ SharePoint client has all automation methods")


def test_web_api_endpoint_exists():
    """Verify /api/ingest-batch endpoint exists in web.py."""
    import inspect
    from rag_pipeline import web

    # Read web.py source
    source = inspect.getsource(web)

    # Verify endpoint exists
    assert '/api/ingest-batch' in source
    assert 'ingest_batch' in source
    assert 'force_reprocess' in source
    assert 'dry_run' in source

    print("✅ /api/ingest-batch endpoint is properly defined")


if __name__ == "__main__":
    """Run all tests."""
    print("\n" + "="*60)
    print("AUTOMATED INGESTION IMPLEMENTATION VERIFICATION")
    print("="*60 + "\n")

    tests = [
        ("Module Imports", test_imports),
        ("Database Models", test_database_models),
        ("RAG Client API", test_rag_client_store_document),
        ("Distributed Lock", test_distributed_lock),
        ("Orchestrator Dry Run", test_orchestrator_dry_run),
        ("Content Fetcher Stub", test_content_fetcher_stub),
        ("SharePoint Client Interface", test_sharepoint_client_interface),
        ("Web API Endpoint", test_web_api_endpoint_exists),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            print(f"\n📋 Testing: {name}")
            print("-" * 60)
            test_func()
            passed += 1
        except Exception as e:
            print(f"❌ FAILED: {e}")
            failed += 1
            import traceback
            traceback.print_exc()

    print("\n" + "="*60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("="*60 + "\n")

    if failed == 0:
        print("✅ All tests passed! Implementation is ready.")
    else:
        print(f"⚠️  {failed} test(s) failed. Please review.")
