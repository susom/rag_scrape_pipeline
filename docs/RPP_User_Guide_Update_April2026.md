# RAG Preparation Pipeline (RPP) - User Guide

> **Instructions:** This is the full updated content for the SharePoint page
> [RAG-Preparation-Pipeline-(RPP).aspx](https://stanfordhealthcare.sharepoint.com/teams/SchoolofMedicineREDCap-grp/SitePages/RAG-Preparation-Pipeline-(RPP).aspx).
>
> **Last SharePoint update:** March 16, 2026
> **This document covers changes through:** April 14, 2026
>
> Sections marked with 🆕 are new. All other sections are unchanged from the existing page.
> Copy the 🆕 sections into the SharePoint page editor after the "KEY FIELDS" table and before the "REDCap AI Ecosystem" card.
> Also update the **Overview** key features list with the new bullets marked below.

---

## Overview

The RPP is a web-based tool that converts raw content (web pages, PDFs, Word documents) into clean, structured JSON optimized for ingestion into our RAG (Retrieval-Augmented Generation) system.

Key Features:

- Batch processing of web URLs and documents
- AI-powered content extraction (removes navigation, ads, cruft while preserving policy content)
- Source-aware processing (different strategies for web pages vs. uploaded documents)
- Link following (scrape referenced URLs and attachments automatically)
- Deterministic, reproducible outputs with provenance tracking
- SharePoint integration for input/output storage
- 🆕 Multi-site ingestion — configure multiple SharePoint sites with independent credentials
- 🆕 Automated nightly ingestion pipeline with delta detection and version tracking
- 🆕 SharePoint Content Status tracker — real-time visibility into ingestion state
- 🆕 Clean-slate reset endpoint for development and testing

---

*[Existing sections: "Networking Diagram & GCP Config", "How It Works", "AI Content Filtering Philosophy", "Using the Web Interface", "Output Format", "KEY FIELDS" — no changes]*

---

## 🆕 Automated Ingestion Pipeline

RPP includes a fully automated ingestion pipeline that runs on a scheduled basis (nightly via Cloud Scheduler). The pipeline fetches approved documents from SharePoint document libraries, processes them through AI extraction, ingests vectors into Pinecone via the REDCap RAG EM API, and tracks status in a SharePoint list.

Key capabilities:

- Scheduled nightly ingestion (Cloud Scheduler → Cloud Run)
- Multi-site support — configure multiple SharePoint sites with independent credentials
- Delta detection — skip unchanged documents using hash comparison and modification timestamps
- Version tracking — increment document versions on re-ingestion, maintain full history in MySQL
- SharePoint Content Status List — real-time tracking of ingestion state per document
- Stale vector cleanup — automatically removes old vectors when documents are re-processed
- Distributed locking — prevents concurrent runs across Cloud Run instances

### How It Works

```
┌─────────────────────────────────────────────────────┐
│              Cloud Scheduler (nightly)               │
│                                                     │
│  POST /api/ingest-batch?site=rexi&days_back=1       │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│               ACQUIRE DISTRIBUTED LOCK              │
│                                                     │
│  • Prevents concurrent runs across instances        │
│  • Auto-expires after timeout (default: 60 min)     │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│            FETCH FROM SHAREPOINT                    │
│                                                     │
│  • Scan configured document libraries               │
│  • Filter by approval status ("Approved" only)      │
│  • Filter by modification date (days_back)          │
│  • Also fetch external-urls.txt (always)            │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│               DELTA DETECTION                       │
│                                                     │
│  • SP files: compare lastModifiedDateTime → DB      │
│  • External URLs: scrape → SHA-256 hash → DB        │
│  • Skip unchanged documents entirely                │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│           AI EXTRACTION + INGESTION                 │
│                                                     │
│  • Text extraction → Sliding window → AI cleanup    │
│  • Ingest sections into Pinecone (via RAG EM API)   │
│  • Clean up stale vectors from previous versions    │
│  • Update MySQL tracking state                      │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│         UPDATE SHAREPOINT TRACKER LIST              │
│                                                     │
│  • Create or update entry per document              │
│  • Increment RExI Version on re-ingestion           │
│  • Populate metadata (Last Editor, Approver, etc.)  │
│  • Only update on successful ingestion              │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              RELEASE LOCK                           │
└─────────────────────────────────────────────────────┘
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ingest-batch` | POST | Trigger ingestion run |
| `/api/ingest-batch?site=rexi` | POST | Ingest from a specific named site |
| `/api/ingest-batch?force_reprocess=true` | POST | Force re-process all documents (ignore delta detection) |
| `/api/ingest-batch?days_back=7` | POST | Only fetch documents modified in last 7 days |
| `/api/reset-ingestion?confirm=true` | POST | Wipe all ingestion data (see Reset section below) |

---

### 🆕 Multi-Site Ingestion

The pipeline supports ingesting content from multiple SharePoint sites, each with their own credentials, document libraries, and tracker lists. Sites are configured via environment variables and selected at runtime using the `?site=` query parameter.

**Configuration per site:**

- **Hostname** — SharePoint site hostname (e.g., `office365stanford.sharepoint.com`)
- **Site path** — Path to the SharePoint site (e.g., `/sites/RExI`)
- **Content source** — `document_library` (fetch from specific libraries) or `shared_documents` (fetch from Shared Documents folder)
- **Library drive IDs** — Comma-separated list of SharePoint drive IDs to scan for approved documents
- **Tracker list ID** — SharePoint list ID for the Content Status tracker
- **Per-site credentials** — Each site can have its own Azure AD tenant, client ID, and client secret

**Example usage:**

```
POST /api/ingest-batch?site=rexi&force_reprocess=true
```

The default site (no `?site=` parameter) uses the primary environment variables. Named sites use `SHAREPOINT_SITE_{NAME}_*` prefixed variables.

**Environment variable pattern for named sites:**

| Variable | Example |
|----------|---------|
| `SHAREPOINT_SITE_REXI_HOSTNAME` | `office365stanford.sharepoint.com` |
| `SHAREPOINT_SITE_REXI_PATH` | `/sites/RExI` |
| `SHAREPOINT_SITE_REXI_CONTENT_SOURCE` | `document_library` |
| `SHAREPOINT_SITE_REXI_LIBRARY_DRIVE_IDS` | `b!BXW...,b!jSJ...` |
| `SHAREPOINT_SITE_REXI_TRACKER_LIST_ID` | `ab779dfe-32d3-4f66-...` |
| `SHAREPOINT_SITE_REXI_TENANT_ID` | `396573cb-f378-...` |
| `SHAREPOINT_SITE_REXI_CLIENT_ID` | `329f031c-27e0-...` |
| `SHAREPOINT_SITE_REXI_CLIENT_SECRET` | *(stored in GCP Secret Manager)* |

---

### 🆕 SharePoint Content Status Tracker

Each ingestion run updates a SharePoint list ("RExI Content Status List" or equivalent per site) that provides real-time visibility into what has been ingested. The tracker list shows one row per document with the following fields:

| Field | Description |
|-------|-------------|
| Document Title | Hyperlinked title pointing to the source document |
| Content Section | Which document library the file came from |
| Document Link | Direct URL to the SharePoint file |
| Last Editor | The person who last edited the document content (via Activities API) |
| Document Modified | Last modification timestamp from SharePoint |
| Document Created | Original creation timestamp |
| Approver | Person who approved the document (if approval workflow enabled) |
| RExI Version | Incremented each time the document is re-ingested |
| Summary | AI-generated content summary |
| Ingestion Date | When the document was last processed by RPP |

**Deduplication:** The tracker uses a 3-tier matching strategy to prevent duplicate entries on re-ingestion:

1. **URL match** (primary) — matches by document URL in the hyperlinked title field
2. **Rich text href extraction** — parses HTML href attributes from the title field
3. **Title fallback** — matches by plain text title if URL matching fails

**Last Editor detection:** Uses the SharePoint Activities API (`/drives/{id}/items/{id}/activities`) to identify the actual content editor. This is necessary because SharePoint's approval workflow collapses minor version history, attributing all changes to the approver rather than the original editor.

---

### 🆕 Reset Ingestion Endpoint

For development and testing, RPP provides a reset endpoint that performs a clean-slate wipe of all ingestion data:

```
POST /api/reset-ingestion?confirm=true
```

This endpoint:

1. Deletes all vectors from Pinecone (via RAG EM `deleteDocument` API)
2. Removes all rows from the MySQL tracking database
3. Clears all items from the SharePoint Content Status tracker list

⚠️ **Warning:** This is a destructive operation. It is protected by the `confirm=true` parameter and intended for development/testing use only.

---

### 🆕 Change Log (since March 2026)

| Date | Change |
|------|--------|
| 2026-04-14 | **Tracker dedup fix** — Replaced title+content_section matching with 3-tier URL/href/title strategy. Prevents duplicate tracker entries on re-ingestion. |
| 2026-04-14 | **Last Editor via Activities API** — Fixed Last Editor field to use SharePoint Activities API instead of version history. Correctly identifies the actual editor even after approval workflows collapse minor versions. |
| 2026-04-14 | **Reset ingestion endpoint** — Added `POST /api/reset-ingestion` for clean-slate wipes of Pinecone vectors, MySQL tracking rows, and SharePoint tracker list items. |
| 2026-04-14 | **CI/CD fix** — Changed `STORAGE_MODE` from GCP Secret Manager reference to plain environment variable in GitHub Actions workflow. |
| 2026-04-09 | **SharePoint tracker metadata** — Added rich metadata to tracker list entries including Last Editor, Approver, Document Created/Modified timestamps, and AI-generated summaries. |
| 2026-04-09 | **SharePoint ingestion tracking** — Full integration with SharePoint Content Status List for real-time ingestion visibility. Tracker updates only on successful ingestion. |
| 2026-04-08 | **Database namespace uniqueness** — Added migration for unique constraint on (title, source_url, rag_namespace) to prevent cross-namespace collisions. |
| 2026-03-27 | **Multi-site ingestion** — Added `?site=` parameter to `/api/ingest-batch` for ingesting from multiple SharePoint sites with independent credentials and configuration. |
| 2026-03-27 | **Per-site credentials** — Each SharePoint site can have its own Azure AD tenant, client ID, and client secret via `SHAREPOINT_SITE_{NAME}_*` environment variables. |
