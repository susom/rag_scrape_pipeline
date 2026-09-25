# RExI pipeline — GKE CronJob deploy

One-shot pod that runs the SharePoint → AI Hub → pgvector ingestion for the
**RExI** site and exits. Lives in the `rexi` namespace of the `rexi-cluster`
(Autopilot, us-west1), reusing the `gke-rexi-sa` Workload Identity SA, the
private DB DNS (`rexi.db.internal`), and IAM database auth — exactly like the
`rexi-app` Deployment.

## What runs

```
python -m rag_pipeline.ingest_batch --site rexi
```

`ingest_batch.py`: init DB (no DDL) → acquire DistributedLock → scan all approved
docs in the nine RExI workflow libraries → compare each revision with this
environment's successful checkpoint → extract changed/new/retryable docs via
AI Hub → embed + INSERT into `rexi.rag_chunk` → save ingestion state and pending
tracker updates. The designated writer delivers tracker updates; dev never does.

The nightly schedule is explicitly 09:00 UTC. Do not add `--days-back 1` to the
routine job: a 24-hour cutoff misses older approved content in a new environment
or after an outage.

The batch exits nonzero on fatal, per-document, discovery, or write-back errors,
so Kubernetes does not mark a partially failed run successful.

### Environment-specific configuration

This reference manifest targets **dev**, not UAT. Keep
`SHAREPOINT_WRITEBACK_ENABLED=false` here. Each environment needs its own
`DB_HOST`, `DB_NAME`, `DB_SCHEMA`, DB identity/credentials and matching pgvector
destination (`PGVECTOR_TABLE` / `PGVECTOR_NAMESPACE`). Reuse the same nine source
drive IDs and tracker list, not the same ingestion-state database.

Only UAT should set `SHAREPOINT_WRITEBACK_ENABLED=true` for now. When prod takes
over, disable UAT's flag before enabling prod's. The flag gates writes, not
ingestion: all environments maintain their own RAG data. Enabling it also
delivers any pending tracker updates saved while it was disabled.

Start every new environment with write-back disabled and the CronJob suspended.
Verify the database identity/schema/grants, source discovery, ingestion, repeat
deduplication, and chatbot retrieval before resuming the schedule or enabling a
designated writer. Do not infer readiness from a completed image rollout alone.

Write-back first updates the central Content Status List, then copies its date,
status, and version to the source library's `RExIUpdated`, `RExISuccess`, and
`RExIVersion`. Only the central version increments. Mirror failures stay pending
and retry without another increment. Source-text hashes distinguish genuine
content changes from the resulting metadata edits; no new schema beyond
migration 004 is needed. Old tracker-only pending payloads are upgraded on a
full flagged run after verifying that the source revision has not changed.

The only published statuses are **Success** and **Keep Trying**. Success advances
the successful dates/version. Partial, failed, or empty extraction attempts publish
Keep Trying centrally and on the file, changing only status and preserving any
prior successful dates/version. A first-ever failure has no central success
dates/version. Detailed errors and retry-limit states remain internal. Failure
delivery retries are durable too, and cannot overwrite a newer central success.

Before enabling UAT broadly, verify a single file's central/mirrored values and
approval state. A metadata write that changes approval is reported as an error
and is never followed by automatic publishing/re-approval.

Toggles (all in `configmap.yaml`): `AI_BACKEND=aihub`, `RAG_BACKEND=pgvector`,
`DB_ENGINE=postgresql` + `DB_IAM_AUTH=true` + `DB_SKIP_INIT_DDL=true`. With these
unset the same image behaves as the SOM/REDCap default (SecureChatAI + Pinecone +
MySQL), so nothing here affects the live SOM leg.

## Files

| File | Purpose |
|------|---------|
| `db_readiness.sql` | Run once in Cloud SQL Studio as `rexi_owner` (creates `ingestion_locks`, grants). |
| `configmap.yaml` | Non-secret env (AI Hub URLs, DB host, SharePoint site + library drive IDs, tracker list). |
| `secret.example.yaml` | Optional plain-Secret example for standalone installations without CSI; not used by dev. |
| `cronjob.yaml` | The CronJob (schedule, SA, command, env wiring). |

## One-time prerequisites

### 1. DB readiness (you, in Cloud SQL Studio as `rexi_owner`)
Paste the contents of [`db_readiness.sql`](./db_readiness.sql). It creates
`rexi.ingestion_locks` and grants schema/table/sequence privileges directly to
the pod's IAM DB user (`gke-rexi-sa@som-rit-phi-rexi-dev.iam`). The other two
tables (`rag_chunk`, `document_ingestion_state`) already exist.

The readiness SQL also adds the four checkpoint/write-back columns required by
migration 004. Apply it **before deploying the updated image**, using the owner
identity for each environment. Alternatively run
`python -m rag_pipeline.database.migrations.004_add_ingestion_checkpoints` with
that environment's DB configuration and DDL credentials. Existing completed
documents will be reprocessed once to establish source-revision checkpoints.

> Object privileges are granted directly to the IAM user rather than via
> `GRANT rexi_app TO <iam user>` — that role-membership grant needs ADMIN on
> `rexi_app` (superuser only), and Studio runs the batch in one transaction so a
> failure there rolls back everything. If you'd rather manage one role, run the
> commented-out membership grant as the `postgres` superuser instead.

### 2. Image (handled by CI — no manual build)
The `rag_scrape_pipeline` GitHub Action (`.github/workflows/push_docker.yaml`)
builds and pushes the amd64 image on every push to `main`:

```
us-west1-docker.pkg.dev/som-rit-infrastructure-prod/rexi-rag-pipeline/rag-pipeline
```

tagged `latest`, `build-<run#>`, `sha-<sha>`. The `rexi-cluster` node SA already
pulls from this registry (same project as `rexi-app`'s image). Nothing to do
here beyond merging to `main`.

REDCap production uses a separate, manually dispatched workflow. A push to
`main` publishes the RExI image without redeploying REDCap Cloud Run.

### 3. Preserve the existing CSI secret mount
Dev already uses the `secret-provider` SecretProviderClass, shared with the
RExI app. Preserve its read-only mount at `/var/secrets`; do not create a second
`rag-pipeline-secrets` Secret or copy credentials between projects.

The loader reads `/var/secrets/secret.properties` and maps `ai.api.key` to
`AI_HUB_API_KEY` and `sharepoint.client.secret` to
`SHAREPOINT_SITE_REXI_CLIENT_SECRET`. Keep `SHAREPOINT_WRITEBACK_ENABLED=false`
in the ConfigMap, not in Secret Manager. For another environment, provision
its own CSI secret access and database identity before deploying.

## Deploy (Flux GitOps — not `kubectl apply`)

The `rexi` namespace is reconciled by Flux from
[`susom/rexi-deploy`](https://github.com/susom/rexi-deploy/tree/main/som-rit-phi-rexi-dev).
The canonical CronJob + ConfigMap manifest lives there as `rag-pipeline.yaml`
(corresponds to `configmap.yaml` + `cronjob.yaml` in this dir). To deploy, update
that manifest with the intended immutable image tag and configuration; Flux
reconciles committed changes. Preserve its CSI mount, service account, and
pod security settings. No secret values belong in Git.

These local manifests are reference files, not the live deployment source.
The live Flux manifest may pin an older image; a pipeline commit alone does not
prove that the cluster has received it. Verify the reconciled image and config.

### UAT and prod promotion

Build once and promote the verified immutable `build-N` tag, not a fresh image
per environment. Dev follows the Flux image policy automatically; UAT/prod
should pin their approved tag. The dev and UAT manifests live under their
respective project directories in `susom/rexi-deploy`. When prod is provisioned,
add its environment directory to that repository using the same CronJob/CSI
pattern and its actual database IAM identity and infrastructure settings.

Use `RAG_NAMESPACE_OVERRIDE=rexi_knowledge`, `PGVECTOR_NAMESPACE=rexi_knowledge`,
and `PGVECTOR_TABLE=rag_chunk` in each environment, but never share their
database instances. Confirm that each environment's `rexi.db.internal` resolves
to its own database. Apply RExI's managed schema (including `ingestion_locks`)
and the runtime grants before the first batch.

Use explicit UTC schedules: dev `0 9 * * *`, UAT `15 9 * * *`, and prod
`30 9 * * *` when activated. Keep future environments suspended until their
smoke checks pass. Promote the image/config while suspended; only then commit
`suspend: false`. Keep all write-back flags false initially; the separate
single-document writer validation gates enabling UAT, and later prod.

The chatbot's `ai.embedding.url` must point to
`text-embedding-3-small/embeddings`, not a chat-completions endpoint. This is
separate from the pipeline's `AI_HUB_EMBEDDING_URL` and must be correct in every
environment for ingested content to be retrievable.

## First run — dry run before real ingest

Once Flux has applied the intended image/config and CSI is ready, exercise
sourcing + dedup without ingesting content or writing SharePoint. The command
still uses a temporary database lock. These `kubectl` commands
need `container.jobs.create` (`roles/container.developer`); if you only have
read access, ask someone who can create Jobs, or just let the nightly schedule
run and watch logs (`kubectl -n rexi logs job/<name> -f`, read-only is enough).

```bash
# Modify the template BEFORE creating a Job; preserve CSI, env, and security.
kubectl -n rexi create job rexi-dryrun --from=cronjob/rag-pipeline-rexi \
  --dry-run=client -o json \
  | python3 -c 'import json,sys; j=json.load(sys.stdin); c=j["spec"]["template"]["spec"]["containers"][0]; c["command"]=["python","-m","rag_pipeline.ingest_batch","--site","rexi","--dry-run"]; c.pop("args",None); print(json.dumps(j))' \
  | kubectl -n rexi create -f -
```

Check logs:
```bash
kubectl -n rexi logs job/<job-name> -f
```

Expected dry-run summary: approved docs detected, none ingested. Then trigger a
real run (`kubectl -n rexi create job rexi-manual --from=cronjob/rag-pipeline-rexi`)
and verify rows land:

```sql
SELECT count(*) FROM rexi.rag_chunk WHERE namespace = 'rexi_knowledge';
SELECT document_id, rag_ingestion_status, rag_last_ingested_at
FROM rexi.document_ingestion_state ORDER BY last_seen_at DESC LIMIT 20;
```

## Notes

- **Embedding model is fixed** at `text-embedding-3-small` (1536-dim) to match
  the vectors RExI queries `rag_chunk` with. Changing it requires re-embedding
  the whole table.
- **GitOps**: the `rexi` namespace is Flux-managed. The CronJob is deployed by
  committing `rag-pipeline.yaml` to `susom/rexi-deploy/som-rit-phi-rexi-dev/`
  (not `kubectl apply`). Flux reconciles the whole directory on merge to `main`.
- **Library drive IDs** in `configmap.yaml` cover the 9 named workflow libraries
  (Prologue, Exploration, Pre-startup, Startup, Regulatory, Budget, Contract,
  Approvals & Awards, Project Activation). Live Graph enumeration confirmed
  these as separate libraries on 2026-09-22. The default "Documents" and "TEST RExI"
  libraries are intentionally excluded.
