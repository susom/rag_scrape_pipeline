import importlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import Integer, MetaData, create_engine, inspect, text
from sqlalchemy.orm import Session

from rag_pipeline.automation import content_fetcher
from rag_pipeline.automation import orchestrator as orchestration
from rag_pipeline.database.models import DocumentIngestionState
from rag_pipeline.ingest_batch import _parse_args
from rag_pipeline.sharepoint.graph_client import SharePointGraphClient
from rag_pipeline.utils.env import sharepoint_writeback_enabled


REVISION = datetime(2026, 9, 1, tzinfo=timezone.utc)
URI = "https://example.com/source.docx"
DOCUMENT_ID = DocumentIngestionState.generate_document_id("source.docx", URI)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    table = DocumentIngestionState.__table__.to_metadata(metadata)
    table.c.id.type = Integer()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def worker(db, monkeypatch):
    monkeypatch.delenv("RAG_NAMESPACE_OVERRIDE", raising=False)
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "false")
    return orchestration.IngestionOrchestrator(db, site_name="rexi")


def add_record(db, **overrides):
    values = {
        "document_id": DOCUMENT_ID,
        "content_hash": DocumentIngestionState.compute_content_hash("text"),
        "rag_namespace": "rexi",
        "url": URI,
        "rag_ingestion_status": "completed",
        "source_modified_at": REVISION,
        "source_attempt_modified_at": REVISION,
        "last_processed_at": REVISION + timedelta(hours=1),
        "rag_last_ingested_at": REVISION + timedelta(hours=1),
    }
    values.update(overrides)
    record = DocumentIngestionState(**values)
    db.add(record)
    db.commit()
    return record


def pipeline_output():
    return {
        "run_id": "test-run",
        "documents": [{
            "doc_id": "canonical-doc",
            "document_id": DOCUMENT_ID,
            "source": {"uri": URI, "type": "sharepoint_file"},
            "sections": [
                {"section_id": "one", "text": "text", "section_hash": "hash1"},
                {"section_id": "two", "text": "more text", "section_hash": "hash2"},
            ],
        }],
    }


def prepare_source(worker):
    worker._source_modified_at[DOCUMENT_ID] = REVISION
    worker._source_uri_to_document_id[URI] = DOCUMENT_ID
    worker._tracker_metadata[DOCUMENT_ID] = {
        "content_section": "Prologue",
        "document_title": "source.docx",
    }


@pytest.mark.parametrize("value,expected", [
    (None, False), ("false", False), ("0", False), ("off", False),
    ("true", True), (" YES ", True), ("1", True),
])
def test_writeback_flag(monkeypatch, value, expected):
    monkeypatch.delenv("SHAREPOINT_WRITEBACK_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", value)
    assert sharepoint_writeback_enabled() is expected


def test_invalid_flag_is_explicit(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "typo")
    with pytest.raises(ValueError, match="must be a boolean"):
        sharepoint_writeback_enabled()


@pytest.mark.parametrize("method,path", [
    ("PATCH", "/sites/site/lists/list/items/1/fields"),
    ("DELETE", "/sites/site/lists/list/items/1"),
    ("POST", "/sites/site/lists/list/items"),
    ("POST", "/sites/site/pages/page/microsoft.graph.sitePage/publish"),
])
def test_graph_mutations_blocked_before_auth(monkeypatch, method, path):
    monkeypatch.delenv("SHAREPOINT_WRITEBACK_ENABLED", raising=False)
    client = SharePointGraphClient("example.sharepoint.com", "/sites/test",
                                   client_id="test", client_secret="test", tenant_id="test")
    token = Mock(side_effect=AssertionError("Must not authenticate"))
    monkeypatch.setattr(client, "_get_access_token", token)
    with pytest.raises(PermissionError):
        client._make_request(method, path)
    token.assert_not_called()


@pytest.mark.parametrize("method,path,enabled", [
    ("GET", "/sites/site/drives", "false"),
    ("POST", "/search/query", "false"),
    ("PATCH", "/sites/site/lists/list/items/1/fields", "true"),
])
def test_graph_reads_and_enabled_writes(monkeypatch, method, path, enabled):
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", enabled)
    client = SharePointGraphClient("example.sharepoint.com", "/sites/test",
                                   client_id="test", client_secret="test", tenant_id="test")
    monkeypatch.setattr(client, "_get_access_token", lambda: "test-token")
    session = Mock()
    session.request.return_value.json.return_value = {"ok": True}
    monkeypatch.setattr("rag_pipeline.sharepoint.graph_client.get_session", lambda: session)
    assert client._make_request(method, path) == {"ok": True}
    session.request.assert_called_once()


@pytest.mark.parametrize("fields,approved", [
    ({"_ApprovalStatus": 3, "_ModerationStatus": 0}, True),
    ({"_ApprovalStatus": 3}, True),
    ({"_ModerationStatus": 0}, True),
    ({"ApprovalStatus": "Approved"}, True),
    ({"_ApprovalStatus": 2, "_ModerationStatus": 0}, False),
    ({}, False),
])
def test_live_approval_shapes(fields, approved):
    assert content_fetcher._is_item_approved(fields, None) is approved


def test_full_scan_default_and_explicit_window():
    assert _parse_args([]).days_back is None
    assert _parse_args(["--days-back", "7"]).days_back == 7
    with pytest.raises(SystemExit):
        _parse_args(["--days-back", "0"])


@pytest.mark.parametrize("args,has_window", [
    ([], False), (["--days-back", "7"], True),
    (["--days-back", "7", "--force-reprocess"], False),
])
def test_cli_passes_expected_window(monkeypatch, args, has_window):
    from rag_pipeline import ingest_batch

    monkeypatch.setattr(ingest_batch, "load_secret_file", lambda: [])
    monkeypatch.setattr(ingest_batch, "init_db", lambda: None)
    monkeypatch.setattr(ingest_batch, "SessionLocal", Mock())
    monkeypatch.setattr(ingest_batch, "DistributedLock", MagicMock())
    run = Mock(return_value=orchestration.IngestionResult(
        "completed", "run", 0, 0, 0, 0, 0, [], False,
    ))
    monkeypatch.setattr(ingest_batch, "run_automated_ingestion", run)
    assert ingest_batch.run(args) == 0
    assert (run.call_args.kwargs["modified_since"] is not None) is has_window


@pytest.mark.parametrize("days_back,force,has_window", [
    (None, False, False), (7, False, True), (7, True, False),
])
def test_api_passes_expected_window(monkeypatch, days_back, force, has_window):
    from rag_pipeline import web
    from rag_pipeline.automation import locking

    monkeypatch.setattr(web, "INGESTION_API_KEY", "")
    monkeypatch.setattr(locking, "DistributedLock", MagicMock())
    run = Mock(return_value=orchestration.IngestionResult(
        "completed", "run", 0, 0, 0, 0, 0, [], False,
    ))
    monkeypatch.setattr(orchestration, "run_automated_ingestion", run)
    web.ingest_batch(Mock(), days_back=days_back, force_reprocess=force, db=Mock())
    assert (run.call_args.kwargs["modified_since"] is not None) is has_window


def test_api_invalid_window_rejected(monkeypatch):
    from fastapi import HTTPException
    from rag_pipeline import web

    monkeypatch.setattr(web, "INGESTION_API_KEY", "")
    with pytest.raises(HTTPException) as error:
        web.ingest_batch(Mock(), days_back=-1, db=Mock())
    assert error.value.status_code == 422


def test_old_approved_file_is_discovered(worker):
    source = content_fetcher.SharePointFile(
        file_id="source", file_name="source.docx", url=URI,
        download_url="https://example.com/download", last_modified=REVISION,
    )
    assert len(worker._detect_changes([], [source], [])) == 1


def test_only_successful_source_revision_deduplicates(worker, db):
    record = add_record(db)
    assert not worker._should_process_sharepoint(DOCUMENT_ID, REVISION, False)
    assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION, True)
    # A modification during the previous run is earlier than its wall-clock completion,
    # but newer than the source revision actually ingested.
    assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION + timedelta(minutes=10), False)
    record.rag_ingestion_status = "failed"
    db.commit()
    assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION, False)


def test_legacy_checkpoint_bootstraps_once(worker, db):
    add_record(db, source_modified_at=None)
    assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION, False)


def test_source_retry_limit_and_new_revision(worker, db):
    add_record(db, rag_ingestion_status="permanently_failed")
    assert not worker._should_process_sharepoint(DOCUMENT_ID, REVISION, False)
    assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION + timedelta(days=1), False)
    assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION, True)


def test_namespaces_ingest_independently(worker, db):
    add_record(db, rag_namespace="another-environment")
    assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION, False)


def test_full_scan_pages_use_success_checkpoints(worker, db):
    page = content_fetcher.SharePointPage(
        page_id="page", title="page", name="page.aspx", url=URI,
        last_modified=REVISION, publishing_level="published",
    )
    page_id = DocumentIngestionState.generate_document_id("page", URI)
    add_record(db, document_id=page_id)
    assert worker._detect_changes([page], [], []) == []


@pytest.mark.parametrize("status,expected", [
    ("completed", False), ("failed", True), ("processing", True),
    ("permanently_failed", False),
])
def test_url_retry_respects_success_and_limit(worker, db, status, expected):
    add_record(db, rag_ingestion_status=status)
    assert worker._should_process_url(DOCUMENT_ID, "text", URI) is expected
    assert worker._should_process_url(DOCUMENT_ID, "changed", URI)


@pytest.mark.parametrize("failures", [0, 1, 2])
def test_success_checkpoints_and_pending_writeback(worker, db, monkeypatch, failures):
    prepare_source(worker)
    responses = [{"vector_id": "v1"}, {"vector_id": "v2"}]
    for index in range(failures):
        responses[index] = RuntimeError("embedding unavailable")
    store = Mock(side_effect=responses)
    tracker = Mock()
    monkeypatch.setattr(orchestration, "store_document", store)
    monkeypatch.setattr(orchestration, "update_tracker_list", tracker)
    stats = worker._ingest_to_rag(pipeline_output())
    record = db.query(DocumentIngestionState).one()
    if failures:
        assert record.last_processed_at is None
        assert record.rag_last_ingested_at is None
        assert record.source_modified_at is None
        pending = json.loads(record.sharepoint_writeback_payload)
        assert pending["summary"] == "Keep Trying"
        assert pending["ingestion_succeeded"] is False
        assert pending["ingestion_date"] is None
        assert pending["increment_version"] is False
        assert record.rag_ingestion_status == "failed"
        assert stats["documents_failed"] == 1
        assert worker._should_process_sharepoint(DOCUMENT_ID, REVISION, False)
    else:
        assert record.last_processed_at is not None
        assert orchestration._ensure_aware(record.source_modified_at) == REVISION
        assert json.loads(record.sharepoint_writeback_payload)["increment_version"] is True
        assert json.loads(record.sharepoint_writeback_payload)["summary"] == "Success"
        assert stats["documents_processed"] == 1
        assert not worker._should_process_sharepoint(DOCUMENT_ID, REVISION, False)
    tracker.assert_not_called()


def test_failed_revision_retains_success_and_reaches_retry_limit(worker, db, monkeypatch):
    prepare_source(worker)
    old_revision = REVISION - timedelta(days=1)
    record = add_record(db, source_modified_at=old_revision, rag_retry_count=2,
                        rag_ingestion_status="failed")
    previous_success = record.last_processed_at
    monkeypatch.setattr(orchestration, "store_document", Mock(side_effect=RuntimeError("failed")))
    worker._ingest_to_rag(pipeline_output())
    assert record.last_processed_at == previous_success
    assert orchestration._ensure_aware(record.source_modified_at) == old_revision
    assert record.rag_ingestion_status == "permanently_failed"
    assert record.rag_retry_count == 3


def test_changed_source_resets_retry_count(worker, db, monkeypatch):
    prepare_source(worker)
    record = add_record(db, source_attempt_modified_at=REVISION - timedelta(days=1),
                        rag_ingestion_status="permanently_failed", rag_retry_count=3)
    monkeypatch.setattr(orchestration, "store_document", Mock(side_effect=RuntimeError("failed")))
    worker._ingest_to_rag(pipeline_output())
    assert record.rag_ingestion_status == "failed"
    assert record.rag_retry_count == 1


def test_writeback_retry_persists_across_sessions_without_reembedding(worker, db, monkeypatch):
    payload = json.dumps({"title": "source", "url": URI, "site_name": "rexi",
                          "ingestion_date": "2026-09-01T01:00:00Z"})
    add_record(db, sharepoint_writeback_payload=payload)
    tracker = Mock(return_value=False)
    store = Mock(side_effect=AssertionError("Must not re-embed"))
    monkeypatch.setattr(orchestration, "update_tracker_list", tracker)
    monkeypatch.setattr(orchestration, "store_document", store)
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    worker._flush_sharepoint_writebacks()
    with Session(db.bind) as next_db:
        record = next_db.query(DocumentIngestionState).one()
        assert record.sharepoint_writeback_payload == payload
        assert record.sharepoint_writeback_error
        next_worker = orchestration.IngestionOrchestrator(next_db, site_name="rexi")
        tracker.return_value = True
        next_worker._flush_sharepoint_writebacks()
        assert record.sharepoint_writeback_payload is None
        assert record.sharepoint_writeback_error is None
    assert tracker.call_count == 2
    assert tracker.call_args_list[0] == tracker.call_args_list[1]
    store.assert_not_called()


@pytest.mark.parametrize("dry_run,enabled", [(True, "true"), (False, "false")])
def test_no_writeback_for_dry_run_or_disabled(worker, db, monkeypatch, dry_run, enabled):
    add_record(db, sharepoint_writeback_payload='{"title":"pending"}')
    worker.dry_run = dry_run
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", enabled)
    tracker = Mock()
    monkeypatch.setattr(orchestration, "update_tracker_list", tracker)
    worker._flush_sharepoint_writebacks()
    tracker.assert_not_called()
    assert db.query(DocumentIngestionState).one().sharepoint_writeback_payload


def test_no_new_documents_still_retries_writeback(worker, db, monkeypatch):
    add_record(db, sharepoint_writeback_payload='{"title":"pending"}')
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    monkeypatch.setattr(worker, "_fetch_content", lambda **kwargs: ([], [], []))
    monkeypatch.setattr(worker, "_reconcile_deletions", lambda: {})
    tracker = Mock(return_value=True)
    monkeypatch.setattr(orchestration, "update_tracker_list", tracker)
    assert worker.run().status == "completed"
    tracker.assert_called_once()


def tracker_client(monkeypatch, ingestion_date="2026-09-01T01:00:00Z"):
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_TRACKER_LIST_ID", "tracker")
    client = Mock()
    client.get_list_items.return_value = [{
        "id": "entry",
        "fields": {"DocumentTitle": "source.docx", "DocumentLink": URI,
                   "IngestionDate": ingestion_date, "RExIVersion": "4", "Summary": "Success"},
    }]
    monkeypatch.setattr(content_fetcher, "_get_sharepoint_client", lambda site: client)
    monkeypatch.setattr(content_fetcher, "_resolve_tracker_field_names", lambda *args: {
        "document_title": "DocumentTitle", "document_link": "DocumentLink",
        "ingestion_date": "IngestionDate", "version": "RExIVersion",
        "summary": "Summary",
    })
    return client


def test_tracker_retry_does_not_increment_again(monkeypatch):
    client = tracker_client(monkeypatch)
    assert content_fetcher.update_tracker_list(
        "source.docx", URI, ingestion_date="2026-09-01T01:00:00+00:00",
        increment_version=True,
    )
    client.update_list_item_fields.assert_not_called()
    client.add_list_item.assert_not_called()
    assert "max_items" not in client.get_list_items.call_args.kwargs


def test_new_ingestion_increments_tracker(monkeypatch):
    client = tracker_client(monkeypatch)
    assert content_fetcher.update_tracker_list(
        "source.docx", URI, ingestion_date="2026-09-02T01:00:00Z",
        increment_version=True,
    )
    assert client.update_list_item_fields.call_args.kwargs["fields"]["RExIVersion"] == "5"


def test_lookup_failure_never_creates_duplicate(monkeypatch):
    client = tracker_client(monkeypatch)
    client.get_list_items.side_effect = RuntimeError("Graph unavailable")
    assert not content_fetcher.update_tracker_list("source.docx", URI)
    client.add_list_item.assert_not_called()


def test_tracker_disabled_before_client_lookup(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "false")
    client = Mock(side_effect=AssertionError("Must not construct Graph client"))
    monkeypatch.setattr(content_fetcher, "_get_sharepoint_client", client)
    assert not content_fetcher.update_tracker_list("source.docx", URI)
    client.assert_not_called()


def test_reset_does_not_touch_sharepoint_when_disabled(monkeypatch):
    from rag_pipeline import web, sharepoint

    monkeypatch.setattr(web, "INGESTION_API_KEY", "")
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "false")
    graph = Mock(side_effect=AssertionError("Must not contact SharePoint"))
    monkeypatch.setattr(sharepoint, "SharePointGraphClient", graph)
    db = Mock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.delete.return_value = 0
    result = web.reset_ingestion(Mock(), confirm=True, site="rexi", db=db)
    assert result["tracker_cleanup_skipped"] is True
    assert result["tracker_items_deleted"] == 0
    assert result["errors"] == []
    graph.assert_not_called()


def test_missing_tracker_date_column_fails_without_mutation(monkeypatch):
    client = tracker_client(monkeypatch)
    monkeypatch.setattr(content_fetcher, "_resolve_tracker_field_names", lambda *args: {})
    assert not content_fetcher.update_tracker_list(
        "source.docx", URI, ingestion_date="2026-09-01T01:00:00Z", increment_version=True,
    )
    client.update_list_item_fields.assert_not_called()
    client.add_list_item.assert_not_called()


def test_disabled_dry_run_changes_no_local_state(worker, db, monkeypatch):
    record = add_record(db)
    worker.dry_run = True
    original_last_seen = record.last_seen_at
    page = content_fetcher.SharePointFile(
        file_id="source", file_name="source.docx", url=URI,
        download_url="https://example.com/download", last_modified=REVISION,
    )
    monkeypatch.setattr(worker, "_fetch_content", lambda **kwargs: ([], [page], []))
    monkeypatch.setattr(worker, "_reconcile_deletions", lambda: {})
    process = Mock(side_effect=AssertionError("Dry run must not extract"))
    monkeypatch.setattr(worker, "_process_documents", process)
    assert worker.run(force_reprocess=True).dry_run
    assert record.last_seen_at == original_last_seen
    process.assert_not_called()


def test_failed_ingestion_commit_cannot_deliver_tracker(worker, db, monkeypatch):
    prepare_source(worker)
    monkeypatch.setattr(orchestration, "store_document", Mock(return_value={"vector_id": "v"}))
    tracker = Mock()
    monkeypatch.setattr(orchestration, "update_tracker_list", tracker)
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    with monkeypatch.context() as scoped:
        scoped.setattr(db, "commit", Mock(side_effect=RuntimeError("commit unavailable")))
        stats = worker._ingest_to_rag(pipeline_output())
    assert stats["documents_processed"] == 0
    assert stats["documents_failed"] == 1
    assert db.query(DocumentIngestionState).count() == 0
    worker._flush_sharepoint_writebacks()
    tracker.assert_not_called()


def test_checkpoint_migration_is_idempotent():
    migration = importlib.import_module(
        "rag_pipeline.database.migrations.004_add_ingestion_checkpoints"
    )
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE document_ingestion_state (id INTEGER PRIMARY KEY)"))
    migration.run_migration(engine)
    migration.run_migration(engine)
    names = {column["name"] for column in inspect(engine).get_columns("document_ingestion_state")}
    assert names == {
        "id", "source_modified_at", "source_attempt_modified_at",
        "sharepoint_writeback_payload", "sharepoint_writeback_error",
    }
    engine.dispose()


def test_empty_extraction_queues_retry_status_without_success_checkpoint(worker, db):
    prepare_source(worker)
    output = pipeline_output()
    output["documents"][0]["sections"] = []
    output["documents"][0]["errors"] = ["Download/extraction unavailable"]
    stats = worker._ingest_to_rag(output)
    record = db.query(DocumentIngestionState).one()
    payload = json.loads(record.sharepoint_writeback_payload)
    assert stats["documents_failed"] == 1
    assert stats["documents_processed"] == 0
    assert record.source_modified_at is None
    assert record.rag_last_ingested_at is None
    assert record.rag_ingestion_status == "failed"
    assert "Download/extraction unavailable" in record.rag_error_message
    assert payload["summary"] == "Keep Trying"
    assert payload["ingestion_succeeded"] is False
    assert payload["increment_version"] is False
    assert payload["ingestion_date"] is None


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["missing_url", "download", "extraction", "empty"])
def test_file_processing_failures_reach_retry_outbox(worker, db, monkeypatch, tmp_path, existing, failure):
    monkeypatch.chdir(tmp_path)
    if existing:
        add_record(db)
    source = content_fetcher.SharePointFile(
        file_id="file", drive_id="drive", file_name="source.docx", url=URI,
        download_url=None if failure == "missing_url" else "https://example.com/download",
        last_modified=REVISION + timedelta(hours=2),
    )
    client = Mock()
    client.download_file_content.return_value = b"test document"
    if failure == "download":
        client.download_file_content.side_effect = RuntimeError("download unavailable")
    extract = Mock(return_value="")
    if failure == "extraction":
        extract.side_effect = ValueError("unreadable document")
    monkeypatch.setattr(worker, "_get_sp_client", lambda: client)
    monkeypatch.setattr(orchestration, "extract_text_from_file", extract)
    parser = Mock()
    monkeypatch.setattr(orchestration, "SlidingWindowParser", parser)
    store = Mock()
    monkeypatch.setattr(orchestration, "store_document", store)

    queued = worker._detect_changes([], [source], [])
    assert len(queued) == 1
    output = worker._process_documents(queued)
    assert worker._ingest_to_rag(output)["documents_failed"] == 1
    record = db.query(DocumentIngestionState).one()
    payload = json.loads(record.sharepoint_writeback_payload)
    assert record.document_id == DOCUMENT_ID
    assert payload["summary"] == "Keep Trying"
    assert payload["source"]["drive_id"] == "drive"
    assert payload["source"]["content_hash"] is None
    assert payload["ingestion_date"] is None
    assert record.rag_error_message
    assert orchestration._ensure_aware(record.source_modified_at) == (REVISION if existing else None)
    store.assert_not_called()
    parser.return_value.process_file.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "permanently_failed"])
def test_failed_records_can_deliver_pending_retry_status(worker, db, monkeypatch, status):
    record = add_record(db, rag_ingestion_status=status, sharepoint_writeback_payload=json.dumps({
        "title": "source", "url": URI, "ingestion_succeeded": False,
        "attempted_at": REVISION.isoformat(), "summary": "Keep Trying",
    }))
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    pending = record.sharepoint_writeback_payload
    tracker = Mock(return_value=False)
    monkeypatch.setattr(orchestration, "update_tracker_list", tracker)
    worker._flush_sharepoint_writebacks()
    tracker.assert_called_once()
    assert record.sharepoint_writeback_payload == pending
    assert record.sharepoint_writeback_error
    tracker.return_value = True
    worker._flush_sharepoint_writebacks()
    assert tracker.call_count == 2
    assert tracker.call_args.kwargs["ingestion_succeeded"] is False
    assert record.rag_ingestion_status == status
    assert record.sharepoint_writeback_payload is None
    assert record.sharepoint_writeback_error is None


def test_retry_mirror_metadata_does_not_reset_retry_limit(worker, db, monkeypatch):
    add_record(db, rag_ingestion_status="permanently_failed", rag_retry_count=3)
    source = content_fetcher.SharePointFile(
        file_id="file", file_name="source.txt", url=URI, drive_id="drive",
        download_url="https://example.com/download",
        last_modified=REVISION + timedelta(hours=2),
    )
    client = Mock()
    client.download_file_content.return_value = b"text"
    monkeypatch.setattr(worker, "_get_sp_client", lambda: client)
    assert not worker._should_process_sharepoint_file(DOCUMENT_ID, source, False)
    record = db.query(DocumentIngestionState).one()
    assert record.rag_retry_count == 3
    assert orchestration._ensure_aware(record.source_modified_at) == REVISION


def test_metadata_only_change_does_not_reset_active_retry_count(worker, db, monkeypatch):
    prepare_source(worker)
    worker._source_modified_at[DOCUMENT_ID] = REVISION + timedelta(hours=2)
    worker._file_content_hashes[DOCUMENT_ID] = DocumentIngestionState.compute_content_hash("text")
    record = add_record(db, rag_ingestion_status="failed", rag_retry_count=2)
    monkeypatch.setattr(orchestration, "store_document", Mock(side_effect=RuntimeError("embedding unavailable")))
    worker._ingest_to_rag(pipeline_output())
    assert record.rag_retry_count == 3
    assert record.rag_ingestion_status == "permanently_failed"


def test_retry_status_payload_does_not_leak_errors_to_sharepoint(worker, db, monkeypatch):
    prepare_source(worker)
    monkeypatch.setattr(orchestration, "store_document", Mock(side_effect=RuntimeError("private diagnostic")))
    worker._ingest_to_rag(pipeline_output())
    record = db.query(DocumentIngestionState).one()
    assert "private diagnostic" in record.rag_error_message
    assert "private diagnostic" not in record.sharepoint_writeback_payload
    assert json.loads(record.sharepoint_writeback_payload)["summary"] == "Keep Trying"


@pytest.mark.parametrize("dry_run", [False, True])
def test_metadata_only_edit_skips_ai_and_preserves_ingestion_date(worker, db, monkeypatch, dry_run):
    record = add_record(db)
    previous_success = record.last_processed_at
    worker.dry_run = dry_run
    source = content_fetcher.SharePointFile(
        file_id="file", file_name="source.txt", url=URI, drive_id="drive",
        download_url="https://example.com/download",
        last_modified=REVISION + timedelta(hours=2),
    )
    client = Mock()
    client.download_file_content.return_value = b"text"
    monkeypatch.setattr(worker, "_get_sp_client", lambda: client)
    assert not worker._should_process_sharepoint_file(DOCUMENT_ID, source, False)
    assert record.last_processed_at == previous_success
    assert record.sharepoint_writeback_payload is None
    expected = REVISION if dry_run else source.last_modified
    assert orchestration._ensure_aware(record.source_modified_at) == expected


def test_real_content_edit_is_not_hidden_by_source_mirror(worker, db, monkeypatch):
    add_record(db)
    source = content_fetcher.SharePointFile(
        file_id="file", file_name="source.txt", url=URI, drive_id="drive",
        download_url="https://example.com/download", last_modified=REVISION + timedelta(hours=2),
        list_item_fields={"RExISuccess": "Ingested successfully", "RExIVersion": 5},
    )
    client = Mock()
    client.download_file_content.return_value = b"real changed content"
    monkeypatch.setattr(worker, "_get_sp_client", lambda: client)
    assert worker._should_process_sharepoint_file(DOCUMENT_ID, source, False)
    assert worker._file_inputs[DOCUMENT_ID][1] == "real changed content"


def test_pending_source_is_saved_with_pre_ai_hash(worker, db, monkeypatch):
    prepare_source(worker)
    worker._source_files[DOCUMENT_ID] = content_fetcher.SharePointFile(
        file_id="file", file_name="source.txt", url=URI, drive_id="drive",
        download_url="https://example.com/download", last_modified=REVISION,
    )
    fingerprint = DocumentIngestionState.compute_content_hash("original extracted source")
    worker._file_content_hashes[DOCUMENT_ID] = fingerprint
    monkeypatch.setattr(orchestration, "store_document", Mock(return_value={"vector_id": "v"}))
    worker._ingest_to_rag(pipeline_output())
    record = db.query(DocumentIngestionState).one()
    payload = json.loads(record.sharepoint_writeback_payload)
    assert record.content_hash == fingerprint
    assert payload["source"] == {
        "drive_id": "drive", "item_id": "file",
        "content_hash": fingerprint.hex(), "approval_field": None,
    }


@pytest.mark.parametrize("changed", [False, True])
def test_legacy_pending_payload_upgrade_is_safe_and_durable(worker, db, monkeypatch, changed):
    payload = {
        "title": "source.txt", "url": URI, "site_name": "rexi",
        "ingestion_date": "2026-09-01T01:00:00Z", "increment_version": True,
    }
    record = add_record(db, sharepoint_writeback_payload=json.dumps(payload))
    worker._source_files[DOCUMENT_ID] = content_fetcher.SharePointFile(
        file_id="file", file_name="source.txt", url=URI, drive_id="drive",
        download_url="https://example.com/download", last_modified=REVISION,
    )
    client = Mock()
    client.get_drive_item.return_value = {
        "name": "source.txt", "eTag": "file-etag",
        "lastModifiedDateTime": (REVISION + timedelta(days=int(changed))).isoformat(),
        "@microsoft.graph.downloadUrl": "https://example.com/download",
    }
    client.download_file_content.return_value = b"original source text before AI"
    monkeypatch.setattr(worker, "_get_sp_client", lambda: client)
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    tracker = Mock(return_value=False)
    monkeypatch.setattr(orchestration, "update_tracker_list", tracker)
    worker._flush_sharepoint_writebacks()
    if changed:
        tracker.assert_not_called()
        assert "source" not in json.loads(record.sharepoint_writeback_payload)
        assert "revision changed" in record.sharepoint_writeback_error
    else:
        tracker.assert_called_once()
        with Session(db.bind) as persisted:
            row = persisted.query(DocumentIngestionState).one()
            saved = json.loads(row.sharepoint_writeback_payload)
            assert saved["source"]["content_hash"] == row.content_hash.hex()
            assert saved["ingestion_date"] == payload["ingestion_date"]
            assert row.content_hash == DocumentIngestionState.compute_content_hash("original source text before AI")


def test_legacy_upgrade_rejects_edit_during_download(worker, db, monkeypatch):
    record = add_record(db)
    worker._source_files[DOCUMENT_ID] = content_fetcher.SharePointFile(
        file_id="file", file_name="source.txt", url=URI, drive_id="drive",
        download_url="https://example.com/download", last_modified=REVISION,
    )
    client = Mock()
    client.get_drive_item.side_effect = [
        {"name": "source.txt", "eTag": "before", "lastModifiedDateTime": REVISION.isoformat(),
         "@microsoft.graph.downloadUrl": "https://example.com/download"},
        {"eTag": "after"},
    ]
    client.download_file_content.return_value = b"concurrently changed"
    monkeypatch.setattr(worker, "_get_sp_client", lambda: client)
    with pytest.raises(ValueError, match="changed while preparing"):
        worker._upgrade_pending_source(record, {"increment_version": True})
    assert record.sharepoint_writeback_payload is None


def test_dev_manifest_covers_nine_sections_and_full_scan():
    root = Path(__file__).resolve().parents[1]
    config = (root / "deploy/gke/configmap.yaml").read_text()
    value = re.search(r'^  SHAREPOINT_SITE_REXI_LIBRARY_DRIVE_IDS: (".*")$', config, re.M)
    assert value is not None
    drives = json.loads(value.group(1)).split(",")
    assert len(set(drives)) == 9
    assert "b!BXWUfoFePki1FTOIM50Tb7Ru3GLtKONNoLeFVKHt_P1PDV489Zk2TKbjavhRM5mY" in drives
    assert "b!BXWUfoFePki1FTOIM50Tb7Ru3GLtKONNoLeFVKHt_P2fVAhhAKAtR4UzROJQMnrU" in drives
    assert '  SHAREPOINT_WRITEBACK_ENABLED: "false"' in config
    cron = (root / "deploy/gke/cronjob.yaml").read_text()
    assert '  schedule: "0 9 * * *"' in cron
    assert '  timeZone: "Etc/UTC"' in cron
    assert "--days-back" not in cron
    assert 'secretProviderClass: "secret-provider"' in cron
    assert "mountPath: /var/secrets" in cron
    assert "readOnlyRootFilesystem: true" in cron
    assert "secretRef:" not in cron
