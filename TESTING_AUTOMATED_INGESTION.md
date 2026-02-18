# Testing Automated RAG Ingestion

## Test Setup

The stub now returns:
- **1 test document** (`test_document.txt`) with stable content
- **1 test URL** (`https://med.stanford.edu/irt.html`)

## Testing Workflow

### Step 1: Purge Namespace (Fresh Start)

```bash
python test_rag_em_api.py
```

This will test the RAG EM API and purge the namespace.

### Step 2: First Ingestion Run

```bash
curl -X POST "http://localhost:9090/api/ingest-batch" | python -m json.tool
```

**Expected behavior:**
- Both doc + URL are NEW → both processed
- Content hashed and stored in database
- Sections ingested to Pinecone (dense + sparse)
- Vector IDs tracked in database

**Expected response:**
```json
{
  "status": "completed",
  "run_id": "ingest_2026-02-14T...",
  "summary": {
    "documents_processed": 2,
    "sections_ingested": 10,  // depends on content
    "documents_skipped": 0,
    "documents_failed": 0
  }
}
```

### Step 3: Check Database

```bash
docker-compose exec scraper python -c "
from rag_pipeline.database.connection import engine
from sqlalchemy import text

with engine.connect() as conn:
    result = conn.execute(text('''
        SELECT document_id, file_name, url,
               rag_vector_id, rag_ingestion_status,
               sections_processed, sections_total,
               last_processed_at
        FROM document_ingestion_state
        ORDER BY last_processed_at DESC
    '''))

    print('Database Records:')
    print('=' * 80)
    for row in result:
        print(f'Doc: {row[1] or row[2][:50]}')
        print(f'  ID: {row[0]}')
        print(f'  Status: {row[4]}')
        print(f'  Sections: {row[5]}/{row[6]}')
        print(f'  Vector ID: {row[3][:20]}...' if row[3] else '  Vector ID: None')
        print(f'  Processed: {row[7]}')
        print()
"
```

### Step 4: Second Run (No Changes) - Test Skip Logic

```bash
curl -X POST "http://localhost:9090/api/ingest-batch" | python -m json.tool
```

**Expected behavior:**
- Hash matches → BOTH skipped
- No processing, no API calls

**Expected response:**
```json
{
  "status": "completed",
  "summary": {
    "documents_processed": 0,
    "sections_ingested": 0,
    "documents_skipped": 2,  // ← Both skipped!
    "documents_failed": 0
  }
}
```

### Step 5: Modify Test Document

Edit the stub to change the document content:

```bash
# Open the file
code /Users/irvins/Work/content_pipeline/rag_pipeline/automation/content_fetcher.py

# Find the test_doc content (line ~120)
# Add a new line at the end:
"""
... existing content ...

**MODIFIED CONTENT** - This line was added for re-ingestion testing.
"""
```

### Step 6: Third Run (Modified Doc) - Test Re-ingestion

```bash
curl -X POST "http://localhost:9090/api/ingest-batch" | python -m json.tool
```

**Expected behavior:**
- Modified doc → hash mismatch → REPROCESSED ✅
- Same URL → hash match → SKIPPED ✅
- Old vector deleted, new vectors created
- Database updated with new hash + vector ID

**Expected response:**
```json
{
  "status": "completed",
  "summary": {
    "documents_processed": 1,  // ← Only modified doc!
    "sections_ingested": 8,
    "documents_skipped": 1,     // ← URL skipped!
    "documents_failed": 0
  }
}
```

### Step 7: Verify Re-ingestion

Check database again - you should see:
- New `rag_vector_id` for the modified doc
- New `last_processed_at` timestamp
- Same data for the URL (unchanged)

## Customization

**Change the test URL:**
Edit `content_fetcher.py` line ~135:
```python
test_urls = [
    "https://your-test-url.com"
]
```

**Change the test document content:**
Edit `content_fetcher.py` line ~120-130 (the `content=` field)

**Add more test items:**
```python
return [test_doc, test_doc2], [url1, url2, url3]
```

## Cleanup After Testing

Once SharePoint integration is ready, revert stub:

```python
def fetch_content_sources_stub():
    logger.warning("Using stub content fetcher - returning empty lists")
    return [], []
```

And switch orchestrator to use real fetcher:
```python
# In orchestrator.py line ~193
return fetch_content_sources()  # Instead of fetch_content_sources_stub()
```

## Troubleshooting

**No documents processed:**
- Check logs: `docker-compose logs scraper`
- Verify stub is being used (should see "Using stub content fetcher")

**Documents fail to ingest:**
- Check RAG EM is running
- Verify `REDCAP_API_TOKEN` is set
- Test RAG EM API: `python test_rag_em_api.py`

**Hashes don't match on re-run:**
- Content might have trailing whitespace differences
- Check `content_hash` in database vs computed hash

## Success Criteria

After all steps:
- ✅ First run: Both items processed
- ✅ Second run: Both items skipped (hash match)
- ✅ Third run: Only modified doc processed, URL skipped
- ✅ Database has correct hashes, vector IDs, timestamps
- ✅ Old vectors deleted on re-ingestion
- ✅ No errors in logs
