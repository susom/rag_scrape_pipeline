from copy import deepcopy
import hashlib
from unittest.mock import Mock

import pytest
import requests

from rag_pipeline.automation import content_fetcher as fetcher
from rag_pipeline.sharepoint.graph_client import SharePointGraphClient


URL = "https://example.com/source.txt"
INGESTED = "2026-09-22T22:00:00Z"
SOURCE = {
    "drive_id": "drive",
    "item_id": "file",
    "content_hash": hashlib.sha256(b"source text").hexdigest(),
    "approval_field": None,
}
FIELD_NAMES = {
    "document_title": "DocumentTitle",
    "document_link": "DocumentLink",
    "version": "RExIVersion",
    "summary": "Summary",
    "ingestion_date": "IngestionDate",
    "updated": "RExIUpdated",
}


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_TRACKER_LIST_ID", "tracker")
    state = {
        "central": {
            "id": "entry", "eTag": '"central-1"',
            "fields": {
                "DocumentTitle": "source.txt", "DocumentLink": URL,
                "RExIVersion": "4.0", "Summary": "Old status",
                "IngestionDate": "2026-09-01T00:00:00Z",
                "RExIUpdated": "2026-09-01T00:00:00Z",
            },
        },
        "source": {
            "id": "file", "name": "source.txt", "eTag": '"file-1"',
            "@microsoft.graph.downloadUrl": "https://example.com/download",
            "sharepointIds": {"listId": "library"},
            "listItem": {
                "id": "item", "eTag": '"source-1"',
                "fields": {
                    "_ApprovalStatus": 3, "_ModerationStatus": 0,
                    "RExIUpdated": "2026-07-22T00:00:00Z",
                    "RExISuccess": "Pending", "RExIVersion": 1.0,
                },
            },
        },
        "fail_mirror": False,
        "demote_on_write": False,
    }
    client = Mock(spec=SharePointGraphClient)
    client.get_list_items.side_effect = lambda **kwargs: (
        [deepcopy(state["central"])] if state["central"] else []
    )
    client.get_list_item.side_effect = lambda *args: deepcopy(state["central"])
    client.get_drive_item.side_effect = lambda *args: deepcopy(state["source"])
    client.download_file_content.return_value = b"source text"

    def update(list_id, item_id, fields, if_match=None):
        if list_id == "tracker":
            assert item_id == "entry"
            assert if_match == state["central"]["eTag"]
            state["central"]["fields"].update(fields)
            state["central"]["eTag"] += "x"
        else:
            assert list_id == "library" and item_id == "item"
            assert if_match == state["source"]["listItem"]["eTag"]
            if state["fail_mirror"]:
                raise RuntimeError("Source mirror temporarily unavailable")
            state["source"]["listItem"]["fields"].update(fields)
            state["source"]["listItem"]["eTag"] += "x"
            if state["demote_on_write"]:
                state["source"]["listItem"]["fields"]["_ApprovalStatus"] = 2
        return fields

    def create(list_id, fields):
        assert list_id == "tracker"
        state["central"] = {"id": "entry", "eTag": '"new"', "fields": deepcopy(fields)}
        return deepcopy(state["central"])

    client.update_list_item_fields.side_effect = update
    client.add_list_item.side_effect = create
    monkeypatch.setattr(fetcher, "_get_sharepoint_client", lambda site: client)
    monkeypatch.setattr(fetcher, "_resolve_tracker_field_names", lambda *args: FIELD_NAMES)
    return client, state


def deliver(**overrides):
    kwargs = {
        "title": "source.txt", "url": URL, "ingestion_date": INGESTED,
        "summary": "Success", "increment_version": True,
        "source": deepcopy(SOURCE),
    }
    kwargs.update(overrides)
    return fetcher.update_tracker_list(**kwargs)


def test_central_result_is_copied_exactly_to_library(graph):
    client, state = graph
    assert deliver()
    central = state["central"]["fields"]
    mirror = state["source"]["listItem"]["fields"]
    assert central["RExIUpdated"] == central["IngestionDate"] == mirror["RExIUpdated"] == INGESTED
    assert central["Summary"] == mirror["RExISuccess"] == "Success"
    assert central["RExIVersion"] == "5.0"
    assert mirror["RExIVersion"] == 5.0
    calls = client.update_list_item_fields.call_args_list
    assert calls[0].kwargs["list_id"] == "tracker"
    assert calls[1].args[:2] == ("library", "item")
    assert set(calls[1].args[2]) == {"RExIUpdated", "RExISuccess", "RExIVersion"}


def test_new_central_entry_starts_at_one_not_source_default_plus_one(graph):
    client, state = graph
    state["central"] = None
    assert deliver()
    assert state["central"]["fields"]["RExIVersion"] == "1"
    assert state["source"]["listItem"]["fields"]["RExIVersion"] == 1.0
    client.add_list_item.assert_called_once()


def test_partial_delivery_retries_only_mirror_without_another_increment(graph):
    client, state = graph
    state["fail_mirror"] = True
    assert not deliver()
    assert state["central"]["fields"]["RExIVersion"] == "5.0"
    state["fail_mirror"] = False
    assert deliver()
    assert state["central"]["fields"]["RExIVersion"] == "5.0"
    assert state["source"]["listItem"]["fields"]["RExIVersion"] == 5.0
    central_writes = [
        call for call in client.update_list_item_fields.call_args_list
        if call.kwargs.get("list_id") == "tracker"
    ]
    assert len(central_writes) == 1


def test_retry_after_delivery_and_lost_db_commit_performs_no_writes(graph):
    client, _ = graph
    assert deliver()
    client.update_list_item_fields.reset_mock()
    assert deliver()
    client.update_list_item_fields.assert_not_called()


def test_retry_uses_authoritative_central_status_not_stale_payload(graph):
    _, state = graph
    state["fail_mirror"] = True
    assert not deliver()
    state["central"]["fields"]["Summary"] = "Keep Trying"
    state["fail_mirror"] = False
    assert deliver()
    assert state["source"]["listItem"]["fields"]["RExISuccess"] == "Keep Trying"


def test_old_payload_does_not_overwrite_newer_central_result(graph):
    client, state = graph
    state["central"]["fields"].update(
        IngestionDate="2026-09-23T22:00:00Z", RExIUpdated="2026-09-23T22:00:00Z",
        RExIVersion="8", Summary="Success",
    )
    assert deliver()
    assert state["source"]["listItem"]["fields"]["RExIVersion"] == 8.0
    assert state["source"]["listItem"]["fields"]["RExISuccess"] == "Success"
    assert client.update_list_item_fields.call_count == 1


def test_legacy_central_entry_gets_missing_updated_date_without_increment(graph):
    _, state = graph
    state["central"]["fields"]["IngestionDate"] = INGESTED
    del state["central"]["fields"]["RExIUpdated"]
    assert deliver()
    assert state["central"]["fields"]["RExIVersion"] == "4.0"
    assert state["central"]["fields"]["RExIUpdated"] == INGESTED
    assert state["source"]["listItem"]["fields"]["RExIVersion"] == 4.0


@pytest.mark.parametrize("problem", ["content", "approval", "etag", "download"])
def test_stale_or_unverifiable_source_blocks_both_destinations(graph, problem):
    client, state = graph
    if problem == "content":
        client.download_file_content.return_value = b"new document content"
    elif problem == "approval":
        state["source"]["listItem"]["fields"]["_ApprovalStatus"] = 2
    elif problem == "etag":
        del state["source"]["listItem"]["eTag"]
    else:
        del state["source"]["@microsoft.graph.downloadUrl"]
    assert not deliver()
    client.update_list_item_fields.assert_not_called()
    client.add_list_item.assert_not_called()


def test_source_concurrency_conflict_keeps_central_version_for_retry(graph):
    client, state = graph
    original_update = client.update_list_item_fields.side_effect

    def conflict(list_id, item_id, fields, if_match=None):
        if list_id == "library":
            response = requests.Response()
            response.status_code = 412
            raise requests.HTTPError("Source changed", response=response)
        return original_update(list_id, item_id, fields, if_match)

    client.update_list_item_fields.side_effect = conflict
    assert not deliver()
    assert state["central"]["fields"]["RExIVersion"] == "5.0"
    client.update_list_item_fields.side_effect = original_update
    assert deliver()
    assert state["central"]["fields"]["RExIVersion"] == "5.0"


def test_approval_change_is_reported_and_never_auto_published(graph):
    client, state = graph
    state["demote_on_write"] = True
    assert not deliver()
    client.publish_page.assert_not_called()
    assert not deliver()


def test_flag_off_blocks_both_tracker_and_mirror(graph, monkeypatch):
    client, _ = graph
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "false")
    assert not deliver()
    client.get_drive_item.assert_not_called()
    client.update_list_item_fields.assert_not_called()


@pytest.mark.parametrize("version", ["nonsense", "NaN", "Infinity", "-1"])
def test_invalid_central_version_is_not_silently_reset(graph, version):
    client, state = graph
    state["central"]["fields"]["RExIVersion"] = version
    assert not deliver()
    client.update_list_item_fields.assert_not_called()


def test_legacy_error_details_are_not_used_as_sharepoint_status(graph):
    _, state = graph
    assert deliver(summary="Error detail: " + "x" * 256)
    assert state["central"]["fields"]["Summary"] == "Success"
    assert state["source"]["listItem"]["fields"]["RExISuccess"] == "Success"


def test_unconfirmed_central_write_cannot_complete_mirror(graph):
    client, state = graph
    client.update_list_item_fields.side_effect = lambda *args, **kwargs: {}
    assert not deliver()
    assert state["source"]["listItem"]["fields"]["RExISuccess"] == "Pending"
    assert client.update_list_item_fields.call_count == 1


def test_central_concurrency_token_required_even_for_repair(graph):
    client, state = graph
    state["central"]["fields"]["IngestionDate"] = INGESTED
    del state["central"]["fields"]["RExIUpdated"]
    del state["central"]["eTag"]
    assert not deliver()
    client.update_list_item_fields.assert_not_called()


def report_retry(**overrides):
    return deliver(ingestion_succeeded=False, attempted_at=INGESTED, **overrides)


def test_failed_ingestion_changes_only_status_in_both_places(graph):
    client, state = graph
    central_before = deepcopy(state["central"]["fields"])
    source_before = deepcopy(state["source"]["listItem"]["fields"])
    assert report_retry()
    assert state["central"]["fields"] == {**central_before, "Summary": "Keep Trying"}
    assert state["source"]["listItem"]["fields"] == {**source_before, "RExISuccess": "Keep Trying"}
    client.download_file_content.assert_not_called()
    assert client.update_list_item_fields.call_args_list[0].kwargs["fields"] == {"Summary": "Keep Trying"}
    assert client.update_list_item_fields.call_args_list[1].args[2] == {"RExISuccess": "Keep Trying"}


def test_first_attempt_failure_does_not_invent_success_dates_or_version(graph):
    _, state = graph
    state["central"] = None
    previous_source = deepcopy(state["source"]["listItem"]["fields"])
    assert report_retry()
    central = state["central"]["fields"]
    assert central["Summary"] == "Keep Trying"
    assert not any(field in central for field in ("IngestionDate", "RExIUpdated", "RExIVersion"))
    assert state["source"]["listItem"]["fields"] == {**previous_source, "RExISuccess": "Keep Trying"}


def test_retry_status_does_not_require_a_download_or_content_hash(graph):
    client, state = graph
    del state["source"]["@microsoft.graph.downloadUrl"]
    source = {key: value for key, value in SOURCE.items() if key != "content_hash"}
    assert report_retry(source=source)
    client.download_file_content.assert_not_called()


def test_partial_retry_status_delivery_is_idempotent(graph):
    client, state = graph
    state["fail_mirror"] = True
    assert not report_retry()
    assert state["central"]["fields"]["Summary"] == "Keep Trying"
    state["fail_mirror"] = False
    client.update_list_item_fields.reset_mock()
    assert report_retry()
    assert client.update_list_item_fields.call_count == 1
    assert client.update_list_item_fields.call_args.args[:2] == ("library", "item")
    assert state["central"]["fields"]["RExIVersion"] == "4.0"


def test_unconfirmed_retry_status_is_not_treated_as_delivered(graph):
    client, _ = graph
    client.update_list_item_fields.side_effect = lambda *args, **kwargs: {}
    assert not report_retry()
    assert client.update_list_item_fields.call_count == 1


def test_old_failure_cannot_overwrite_later_success(graph):
    client, state = graph
    state["central"]["fields"].update(IngestionDate="2026-09-23T00:00:00Z", Summary="Success")
    assert report_retry()
    client.update_list_item_fields.assert_not_called()


def test_success_after_retry_advances_version_once(graph):
    _, state = graph
    assert report_retry()
    assert state["central"]["fields"]["RExIVersion"] == "4.0"
    assert deliver(ingestion_date="2026-09-23T00:00:00Z")
    assert state["central"]["fields"]["RExIVersion"] == "5.0"
    assert state["central"]["fields"]["Summary"] == "Success"
    assert state["source"]["listItem"]["fields"]["RExISuccess"] == "Success"


def test_first_success_after_failed_first_attempt_starts_at_one(graph):
    _, state = graph
    state["central"] = None
    assert report_retry()
    assert deliver(ingestion_date="2026-09-23T00:00:00Z")
    assert state["central"]["fields"]["RExIVersion"] == "1"
    assert state["source"]["listItem"]["fields"]["RExIVersion"] == 1.0


def test_legacy_success_retry_repairs_label_without_incrementing_version(graph):
    _, state = graph
    state["central"]["fields"].update(
        IngestionDate=INGESTED, RExIUpdated=INGESTED, Summary="Ingested successfully",
    )
    assert deliver(summary="Ingested successfully")
    assert state["central"]["fields"]["Summary"] == "Success"
    assert state["source"]["listItem"]["fields"]["RExISuccess"] == "Success"
    assert state["central"]["fields"]["RExIVersion"] == "4.0"


def test_retry_status_remains_flag_gated(graph, monkeypatch):
    client, _ = graph
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "false")
    assert not report_retry()
    client.get_drive_item.assert_not_called()
    client.update_list_item_fields.assert_not_called()


def test_invalid_central_status_never_reaches_source(graph):
    client, state = graph
    state["central"]["fields"].update(IngestionDate="2026-09-23T00:00:00Z", Summary="Fail")
    assert not deliver()
    client.update_list_item_fields.assert_not_called()


def test_graph_forwards_source_etag(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_WRITEBACK_ENABLED", "true")
    client = SharePointGraphClient(
        "example.sharepoint.com", "/sites/test",
        client_id="test", client_secret="test", tenant_id="test",
    )
    client._site_id = "site"
    monkeypatch.setattr(client, "_get_access_token", lambda: "test-token")
    session = Mock()
    session.request.return_value.status_code = 200
    session.request.return_value.json.return_value = {}
    monkeypatch.setattr("rag_pipeline.sharepoint.graph_client.get_session", lambda: session)
    client.update_list_item_fields("library", "item", {"RExISuccess": "Success"}, if_match='"version-1"')
    assert session.request.call_args.kwargs["headers"]["If-Match"] == '"version-1"'


def test_exhausted_graph_rate_limit_is_not_reported_as_success(monkeypatch):
    client = SharePointGraphClient(
        "example.sharepoint.com", "/sites/test",
        client_id="test", client_secret="test", tenant_id="test",
    )
    monkeypatch.setattr(client, "_get_access_token", lambda: "test-token")
    monkeypatch.setattr("rag_pipeline.sharepoint.graph_client.time.sleep", lambda seconds: None)
    response = requests.Response()
    response.status_code = 429
    response.url = "https://graph.microsoft.com/v1.0/sites/site"
    session = Mock()
    session.request.return_value = response
    monkeypatch.setattr("rag_pipeline.sharepoint.graph_client.get_session", lambda: session)
    with pytest.raises(requests.HTTPError):
        client._make_request("GET", "/sites/site")
    assert session.request.call_count == client.MAX_RETRIES
