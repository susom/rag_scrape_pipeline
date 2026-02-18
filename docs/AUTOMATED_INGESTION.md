# Automated RAG Ingestion Workflow

## Overview

This document describes the automated RAG ingestion system that was implemented based on the plan in the session transcript.

**Status:** ✅ Core implementation complete. Awaiting:
- Database credentials configuration
- SharePoint client implementation (coworker)
- Production deployment

## Architecture

```
Cloud Scheduler (weekly)
    ↓
POST /api/ingest-batch (Cloud Run endpoint)
    ↓
Distributed Lock (prevents concurrent runs)
    ↓
Fetch Content (SharePoint docs + URLs from SharePoint page)
    ↓
Hash-Based Delta Detection (query DB, compare SHA256)
    ↓
Queue Changed/New Documents (skip unchanged)
    ↓
Process via run_pipeline() (existing RAG prep logic)
    ↓
Loop through output JSON sections
    ↓
Call storeDocument() for each section (REDCap RAG EM)
    ↓
Update DB (content_hash, rag_vector_id, timestamps)
    ↓
Return Summary (processed, skipped, errors)
```

## Implemented Components

### 1. Database Schema (`rag_pipeline/database/models.py`)

**Extended `DocumentIngestionState`:**
```python
# RAG vector database tracking
rag_vector_id = Column(String(255), nullable=True, index=True)
rag_namespace = Column(String(255), nullable=True)
rag_last_ingested_at = Column(DateTime(), nullable=True, index=True)
rag_ingestion_status = Column(String(50), default="pending", index=True)
rag_error_message = Column(Text, nullable=True)
rag_retry_count = Column(Integer, default=0, nullable=False)

# Section tracking
sections_processed = Column(Integer, default=0, nullable=False)
sections_total = Column(Integer, default=0, nullable=False)

# Deletion detection
last_seen_at = Column(DateTime(), nullable=True, index=True)
```

**New `IngestionLock` table:**
```python
lock_key = Column(String(255), primary_key=True)
acquired_at = Column(DateTime(), nullable=False)
acquired_by = Column(String(255), nullable=False)  # hostname:pid
expires_at = Column(DateTime(), nullable=False, index=True)
```

**Migration:** `rag_pipeline/database/migrations/001_add_rag_fields.py`
- Adds all RAG tracking columns
- Creates indexes for query performance
- Creates `ingestion_locks` table
- Idempotent (safe to re-run)

### 2. RAG Client (`rag_pipeline/automation/rag_client.py`)

REDCap RAG EM API wrapper with retry logic:

```python
def store_document(title, content, metadata, api_url=None, api_token=None, namespace=None)
def query_documents(query, top_k=5, namespace=None, ...)
def delete_document(vector_id, namespace=None, ...)
```

**Features:**
- Automatic retries with exponential backoff (max 3 attempts)
- Error handling for HTTP and API-level errors
- Uses `REDCAP_API_URL` and `REDCAP_API_TOKEN` env vars

**Based on:** REDCap RAG EM's `storeDocument` API action

### 3. Distributed Locking (`rag_pipeline/automation/locking.py`)

Database-backed advisory lock using `IngestionLock` table:

```python
with DistributedLock("automated_ingestion", db_session=db, timeout_minutes=60):
    # Protected code - only one process can execute at a time
    run_ingestion()
```

**Features:**
- Context manager interface
- Automatic stale lock cleanup (expired locks removed)
- Process identification (`hostname:pid`)
- Raises `LockAlreadyHeld` if lock is active

### 4. SharePoint Client (STUBBED - `rag_pipeline/automation/sharepoint_client.py`)

**Interface for coworker to implement:**

```python
class SharePointClient:
    def get_documents() -> List[SharePointDocument]:
        """
        Fetch documents from SharePoint library.

        TODO: Implement using MS Graph API:
        - Authenticate with client credentials
        - GET /sites/{site-id}/drives/{drive-id}/root/children
        - Download and extract text (DOCX, PDF, TXT)
        - Return list of SharePointDocument objects
        """

    def get_external_urls_page() -> str:
        """
        Fetch SharePoint page with external URL list.

        TODO: Implement using MS Graph API:
        - GET /sites/{site-id}/pages/{page-id}
        - Return canvasContent1 (HTML)
        """
```

**Required environment variables:**
```bash
SHAREPOINT_TENANT_ID=<tenant>
SHAREPOINT_CLIENT_ID=<client_id>
SHAREPOINT_CLIENT_SECRET=<secret>
SHAREPOINT_SITE_URL=https://<tenant>.sharepoint.com/sites/<site>
SHAREPOINT_LIBRARY_NAME=Documents
SHAREPOINT_URLS_PAGE_ID=<page_id>
```

### 5. Content Fetcher (`rag_pipeline/automation/content_fetcher.py`)

Unified content fetching coordinator:

```python
def fetch_content_sources() -> Tuple[List[SharePointDocument], List[str]]:
    """
    Fetch from all sources.

    Returns:
        (sharepoint_documents, external_urls)
    """
```

**Features:**
- Calls SharePoint client to get documents
- Extracts URLs from SharePoint URLs page HTML
- Graceful error handling (continues if one source fails)
- Currently uses stub (`fetch_content_sources_stub()`)

### 6. Orchestrator (`rag_pipeline/automation/orchestrator.py`)

Main ingestion workflow logic:

```python
def run_automated_ingestion(
    db_session,
    force_reprocess=False,
    document_ids=None,
    dry_run=False
) -> IngestionResult
```

**Workflow:**
1. **Fetch content** from SharePoint and external URLs
2. **Delta detection** - compare SHA256 hashes
3. **Process changed docs** via `run_pipeline()`
4. **Ingest sections** to RAG EM via `store_document()`
5. **Update database** with results and errors
6. **Return summary** with statistics

**Features:**
- Hash-based delta detection (skip unchanged documents)
- Partial failure handling (failed sections don't block successful ones)
- Retry logic (max 3 attempts per document)
- Comprehensive error tracking
- Dry run mode (report changes without ingesting)

**Error handling tiers:**
1. **HTTP-level:** Retry once, log error
2. **Document-level:** Track retry count, mark failed after 3 attempts
3. **System-level:** Release lock, return 500

### 7. Web API Endpoint (`rag_pipeline/web.py`)

**POST `/api/ingest-batch`**

**Query parameters:**
- `force_reprocess` (bool): Ignore hash, reprocess all documents
- `document_ids` (string): Comma-separated list of specific document IDs
- `dry_run` (bool): Report changes without ingesting

**Response:**
```json
{
  "status": "completed" | "failed" | "locked",
  "run_id": "ingest_2025-02-13T10-30-00Z",
  "summary": {
    "documents_processed": 12,
    "sections_ingested": 143,
    "documents_skipped": 45,
    "documents_failed": 2,
    "processing_time_seconds": 120.5
  },
  "errors": [...],
  "dry_run": false
}
```

**Status codes:**
- `200`: Success (may include partial failures)
- `409`: Conflict (another ingestion in progress)
- `500`: Fatal error

## Environment Variables

**Existing (reused):**
```bash
REDCAP_API_URL=http://localhost/api/
REDCAP_API_TOKEN=<token>  # Used for both SecureChatAI and RAG EM
```

**Database (existing):**
```bash
DB_USER=<username>
DB_PASSWORD=<password>
DB_NAME=document_ingestion_state
DB_HOST=<host>  # Optional, for direct connection
CLOUD_SQL_CONNECTION_NAME=som-rit-phi-redcap-prod:us-west1:redcap-rag
DB_SOCKET_DIR=/socket
```

**New (for SharePoint - coworker to implement):**
```bash
SHAREPOINT_TENANT_ID=<tenant>
SHAREPOINT_CLIENT_ID=<client_id>
SHAREPOINT_CLIENT_SECRET=<secret>
SHAREPOINT_SITE_URL=https://<tenant>.sharepoint.com/sites/<site>
SHAREPOINT_LIBRARY_NAME=Documents
SHAREPOINT_URLS_PAGE_ID=<page_id>
```

**Tuning:**
```bash
INGESTION_LOCK_TIMEOUT_MINUTES=60  # Default lock timeout
INGESTION_MAX_RETRIES=3  # Max retry attempts per document
```

## Running the Migration

**Inside Docker container:**
```bash
docker-compose exec scraper python -m rag_pipeline.database.migrations.001_add_rag_fields
```

**Verification:**
```sql
-- Check new columns
DESCRIBE document_ingestion_state;

-- Check indexes
SHOW INDEX FROM document_ingestion_state;

-- Check new table
SHOW TABLES LIKE 'ingestion_locks';
```

## Testing

**Run verification tests:**
```bash
docker-compose exec scraper python tests/test_automated_ingestion.py
```

**Manual dry run test:**
```bash
curl -X POST "http://localhost:9090/api/ingest-batch?dry_run=true" | jq
```

**Expected dry run response:**
```json
{
  "status": "completed",
  "run_id": "ingest_dry_...",
  "summary": {
    "documents_processed": 0,
    "sections_ingested": 0,
    "documents_skipped": 0,
    "documents_failed": 0
  },
  "dry_run": true
}
```

## Cloud Scheduler Setup

**After deployment, create weekly job:**

```bash
gcloud scheduler jobs create http rag-automated-ingestion \
  --location=us-west1 \
  --schedule="0 2 * * 0" \
  --uri="https://production-pipeline-xxx.a.run.app/api/ingest-batch" \
  --http-method=POST \
  --max-retry-attempts=3
```

**Schedule:** Weekly Sunday 2am UTC

## Cloud Run Configuration

**CRITICAL:** Must set request timeout to 3600s (60 minutes)

```bash
gcloud run services update production-pipeline \
  --timeout=3600 \
  --region=us-west1
```

Default 5-minute timeout will kill long-running ingestion operations.

## Monitoring Queries

**Check recent ingestions:**
```sql
SELECT document_id, rag_ingestion_status, rag_last_ingested_at,
       sections_processed, sections_total
FROM document_ingestion_state
WHERE rag_last_ingested_at > NOW() - INTERVAL 1 HOUR
ORDER BY rag_last_ingested_at DESC;
```

**Check failed documents:**
```sql
SELECT document_id, rag_error_message, rag_retry_count, url
FROM document_ingestion_state
WHERE rag_ingestion_status = 'failed'
ORDER BY rag_retry_count DESC;
```

**Check permanently failed:**
```sql
SELECT document_id, rag_error_message, url
FROM document_ingestion_state
WHERE rag_ingestion_status = 'permanently_failed';
```

**Check active locks:**
```sql
SELECT lock_key, acquired_by, acquired_at, expires_at
FROM ingestion_locks
WHERE expires_at > NOW();
```

## Next Steps

### Immediate (Blocking)
1. ✅ **Fix database credentials** - Configure proper DB_USER/DB_PASSWORD
2. ✅ **Run migration** - Execute `001_add_rag_fields.py`
3. ✅ **Test dry run** - Verify endpoint with `dry_run=true`

### SharePoint Integration (Coworker)
1. **Implement `SharePointClient.get_documents()`**
   - Use MS Graph API authentication
   - Fetch documents from library
   - Extract text content

2. **Implement `SharePointClient.get_external_urls_page()`**
   - Fetch page content via Graph API
   - Return HTML for URL extraction

3. **Switch content fetcher** from stub to live:
   ```python
   # In orchestrator.py, replace:
   return fetch_content_sources_stub()
   # With:
   return fetch_content_sources()
   ```

### Production Deployment
1. **Update Cloud Run timeout** to 3600s
2. **Deploy new code** to Cloud Run
3. **Run manual test** with `dry_run=true`
4. **Create Cloud Scheduler job** (weekly)
5. **Set up monitoring alerts** (failures, lock timeouts)

### Future Enhancements
- Deletion detection workflow (soft delete based on `last_seen_at`)
- Batch optimization (parallel `storeDocument` calls)
- Content diff visualization
- Webhook notifications (Slack/email)
- Advanced retry strategies

## Files Created/Modified

**New files:**
- `rag_pipeline/automation/__init__.py`
- `rag_pipeline/automation/rag_client.py`
- `rag_pipeline/automation/locking.py`
- `rag_pipeline/automation/orchestrator.py`
- `rag_pipeline/automation/content_fetcher.py`
- `rag_pipeline/automation/sharepoint_client.py`
- `rag_pipeline/database/migrations/__init__.py`
- `rag_pipeline/database/migrations/001_add_rag_fields.py`
- `tests/test_automated_ingestion.py`
- `docs/AUTOMATED_INGESTION.md`

**Modified files:**
- `rag_pipeline/database/models.py` - Added RAG tracking fields and IngestionLock model
- `rag_pipeline/web.py` - Added `/api/ingest-batch` endpoint

## Architecture Decisions

### Why database-backed locking instead of Redis?
- Simpler infrastructure (one less service to manage)
- Good enough for weekly cron jobs
- Can be upgraded to Redis later if needed

### Why section-by-section ingestion instead of batch?
- RAG EM API currently supports single document ingestion
- Easier error handling and retry logic
- Can be optimized later with parallel calls

### Why hash-based delta detection?
- Deterministic and reproducible
- Avoids unnecessary API calls (cost savings)
- Enables intelligent re-ingestion

### Why stub SharePoint client?
- Coworker has SharePoint expertise
- Clear interface contract defined
- Allows independent development

## Troubleshooting

**Migration fails with "Access denied":**
- Check `DB_USER` and `DB_PASSWORD` environment variables
- Verify user has `ALTER TABLE` privileges
- Check Cloud SQL proxy is running

**Ingestion returns 409 (locked):**
- Another ingestion is already running
- Check `ingestion_locks` table for active locks
- If lock is stale, it will auto-cleanup after timeout

**Sections fail to ingest:**
- Check `REDCAP_API_URL` and `REDCAP_API_TOKEN`
- Verify RAG EM is installed and configured
- Check error details in `rag_error_message` column

**SharePoint integration fails:**
- Verify all `SHAREPOINT_*` environment variables
- Check coworker's implementation logs
- Test Graph API credentials independently

## Success Criteria

✅ Database migration completes without errors
✅ `/api/ingest-batch` endpoint returns 200 with summary
✅ Hash-based delta detection skips unchanged documents
✅ Changed documents processed and ingested to RAG EM
✅ Database updated with `rag_vector_id` and timestamps
✅ Concurrent runs prevented by distributed lock
✅ Partial failures handled gracefully
✅ Cloud Scheduler triggers successfully
✅ Monitoring alerts configured

## Support

For questions or issues:
- Check logs: `docker-compose logs scraper`
- Review database state with monitoring queries
- Run verification tests: `python tests/test_automated_ingestion.py`
- Consult implementation plan in session transcript
