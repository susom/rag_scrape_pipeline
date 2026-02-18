# Automated RAG Ingestion - Quick Start Guide

## TL;DR

✅ **Core implementation complete** - All components built and tested (8/8 tests passing)
⏳ **Needs:** Database credentials + Migration + SharePoint integration + Deployment

---

## Immediate Actions (In Order)

### 1. Configure Database Credentials ⚠️

```bash
# Set environment variables (or update .env file)
export DB_USER=your_username
export DB_PASSWORD=your_password
```

### 2. Run Database Migration ⚠️

```bash
# Inside Docker container
docker-compose exec scraper python -m rag_pipeline.database.migrations.001_add_rag_fields
```

**Expected output:**
```
[INFO] Starting migration 001: Add RAG tracking fields
[INFO] Adding RAG tracking fields to document_ingestion_state...
[INFO] Creating indexes for RAG fields...
[INFO] Creating ingestion_locks table...
[INFO] Migration 001 completed successfully!
```

### 3. Verify Migration

```bash
# Check database structure
docker-compose exec scraper python -c "
from rag_pipeline.database.connection import engine
from sqlalchemy import text

with engine.connect() as conn:
    # Check new columns exist
    result = conn.execute(text('DESCRIBE document_ingestion_state'))
    columns = [row[0] for row in result.fetchall()]

    rag_columns = [c for c in columns if 'rag_' in c or 'sections_' in c or 'last_seen' in c]
    print(f'✅ RAG tracking fields: {len(rag_columns)} columns added')

    # Check new table exists
    result = conn.execute(text('SHOW TABLES LIKE \"ingestion_locks\"'))
    if result.fetchone():
        print('✅ ingestion_locks table created')
"
```

### 4. Test Dry Run

```bash
# Test without actually ingesting
curl -X POST "http://localhost:9090/api/ingest-batch?dry_run=true" | jq

# Expected response
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

---

## For Coworker (SharePoint Integration)

### 5. Set SharePoint Environment Variables

```bash
export SHAREPOINT_TENANT_ID=<your_tenant_id>
export SHAREPOINT_CLIENT_ID=<app_client_id>
export SHAREPOINT_CLIENT_SECRET=<app_secret>
export SHAREPOINT_SITE_URL=https://<tenant>.sharepoint.com/sites/<site>
export SHAREPOINT_LIBRARY_NAME=Documents
export SHAREPOINT_URLS_PAGE_ID=<page_id>
```

### 6. Implement SharePoint Client

**File:** `rag_pipeline/automation/sharepoint_client.py`

**What to implement:**
- `_authenticate()` - Use MSAL to get MS Graph access token
- `get_documents()` - Fetch docs from library, extract text
- `get_external_urls_page()` - Fetch page HTML content

**API endpoints to use:**
```
# Get documents
GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives/{drive-id}/root/children

# Get page content
GET https://graph.microsoft.com/v1.0/sites/{site-id}/pages/{page-id}
```

**Libraries needed:**
```bash
pip install msal python-docx pdfplumber
```

### 7. Switch to Live Content Fetcher

**File:** `rag_pipeline/automation/orchestrator.py` (line ~193)

**Change:**
```python
# FROM:
return fetch_content_sources_stub()

# TO:
return fetch_content_sources()
```

### 8. Test SharePoint Integration

```bash
# Test with real data (dry run first)
curl -X POST "http://localhost:9090/api/ingest-batch?dry_run=true" | jq

# Expected: Should now show documents from SharePoint
{
  "status": "completed",
  "summary": {
    "documents_skipped": 25  # <-- Documents found!
  },
  "dry_run": true
}
```

---

## Production Deployment

### 9. Update Cloud Run Timeout ⚠️

```bash
gcloud run services update production-pipeline \
  --timeout=3600 \
  --region=us-west1
```

**Why:** Default 5-minute timeout will kill ingestion operations.

### 10. Deploy Code

```bash
# Push to trigger Cloud Build
git add .
git commit -m "Add automated RAG ingestion workflow"
git push origin db-connect

# Or manual deploy
gcloud run deploy production-pipeline \
  --source . \
  --region us-west1
```

### 11. Create Cloud Scheduler Job

```bash
gcloud scheduler jobs create http rag-automated-ingestion \
  --location=us-west1 \
  --schedule="0 2 * * 0" \
  --uri="https://production-pipeline-xxx.a.run.app/api/ingest-batch" \
  --http-method=POST \
  --max-retry-attempts=3
```

**Schedule:** Weekly Sunday 2am UTC

---

## Common Commands

### Run Tests
```bash
docker-compose exec scraper sh -c "cd /app && PYTHONPATH=/app python tests/test_automated_ingestion.py"
```

### Check Logs
```bash
# Local
docker-compose logs -f scraper

# Production
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=production-pipeline" --limit 50
```

### Manual Ingestion (Production)
```bash
# Dry run first
curl -X POST "https://production-pipeline-xxx.a.run.app/api/ingest-batch?dry_run=true"

# Full ingestion
curl -X POST "https://production-pipeline-xxx.a.run.app/api/ingest-batch"

# Force reprocess all
curl -X POST "https://production-pipeline-xxx.a.run.app/api/ingest-batch?force_reprocess=true"
```

### Database Queries

```sql
-- Check recent ingestions
SELECT document_id, rag_ingestion_status, sections_processed, sections_total
FROM document_ingestion_state
WHERE rag_last_ingested_at > NOW() - INTERVAL 1 HOUR;

-- Check failed documents
SELECT document_id, rag_error_message, rag_retry_count
FROM document_ingestion_state
WHERE rag_ingestion_status IN ('failed', 'permanently_failed');

-- Check active locks
SELECT * FROM ingestion_locks WHERE expires_at > NOW();
```

---

## File Structure

```
rag_pipeline/
├── automation/              # NEW - Automated ingestion modules
│   ├── __init__.py
│   ├── rag_client.py       # REDCap RAG EM API wrapper
│   ├── locking.py          # Distributed lock
│   ├── orchestrator.py     # Main workflow logic
│   ├── content_fetcher.py  # Unified content fetching
│   └── sharepoint_client.py # SharePoint integration (STUB)
├── database/
│   ├── models.py           # MODIFIED - Added RAG fields + IngestionLock
│   └── migrations/         # NEW - Database migrations
│       ├── __init__.py
│       └── 001_add_rag_fields.py
└── web.py                  # MODIFIED - Added /api/ingest-batch endpoint

tests/
└── test_automated_ingestion.py  # NEW - Verification tests

docs/
└── AUTOMATED_INGESTION.md       # NEW - Full documentation

IMPLEMENTATION_SUMMARY.md        # NEW - Implementation summary
QUICKSTART.md                    # NEW - This file
```

---

## Troubleshooting

**Migration fails with "Access denied":**
- Check `DB_USER` and `DB_PASSWORD` are correct
- Verify user has `ALTER TABLE` privileges

**Endpoint returns 409 (locked):**
- Another ingestion is running
- Check `SELECT * FROM ingestion_locks`
- Stale locks auto-cleanup after timeout

**No documents found in dry run:**
- SharePoint integration not yet implemented (expected)
- Check stub is being used (see logs: "Using stub content fetcher")

**Tests fail with "No module named 'rag_pipeline'":**
- Run with: `cd /app && PYTHONPATH=/app python tests/...`

---

## Documentation

- **Full Guide:** `docs/AUTOMATED_INGESTION.md`
- **Implementation Summary:** `IMPLEMENTATION_SUMMARY.md`
- **Original Plan:** Session transcript at `~/.claude/projects/.../1bfecf52-...jsonl`

---

## Support Checklist

Before asking for help:
- [ ] Verified database credentials are correct
- [ ] Ran migration successfully
- [ ] Checked logs: `docker-compose logs scraper`
- [ ] Ran tests: All 8 passing?
- [ ] Consulted `docs/AUTOMATED_INGESTION.md`

---

**Status:** ✅ Ready for database configuration and SharePoint integration
**Test Results:** 8/8 passing
**Next Step:** Configure DB credentials and run migration
