# RExI pipeline — GKE CronJob deploy

One-shot pod that runs the SharePoint → AI Hub → pgvector ingestion for the
**RExI** site and exits. Lives in the `rexi` namespace of the `rexi-cluster`
(Autopilot, us-west1), reusing the `gke-rexi-sa` Workload Identity SA, the
private DB DNS (`rexi.db.internal`), and IAM database auth — exactly like the
`rexi-app` Deployment.

## What runs

```
python -m rag_pipeline.ingest_batch --site rexi --days-back 1
```

`ingest_batch.py`: init DB (no DDL) → acquire DistributedLock → fetch changed
approved docs from the RExI workflow libraries → extract via AI Hub → embed +
INSERT into `rexi.rag_chunks` → update tracker list + `document_ingestion_state`.

Toggles (all in `configmap.yaml`): `AI_BACKEND=aihub`, `RAG_BACKEND=pgvector`,
`DB_ENGINE=postgresql` + `DB_IAM_AUTH=true` + `DB_SKIP_INIT_DDL=true`. With these
unset the same image behaves as the SOM/REDCap default (SecureChatAI + Pinecone +
MySQL), so nothing here affects the live SOM leg.

## Files

| File | Purpose |
|------|---------|
| `db_readiness.sql` | Run once in Cloud SQL Studio as `rexi_owner` (creates `ingestion_locks`, grants). |
| `configmap.yaml` | Non-secret env (AI Hub URLs, DB host, SharePoint site + library drive IDs, tracker list). |
| `secret.example.yaml` | Template for the two secrets — **create the real Secret out-of-band, don't commit values**. |
| `cronjob.yaml` | The CronJob (schedule, SA, command, env wiring). |

## One-time prerequisites

### 1. DB readiness (you, in Cloud SQL Studio as `rexi_owner`)
Paste the contents of [`db_readiness.sql`](./db_readiness.sql). It creates
`rexi.ingestion_locks` and grants schema/table/sequence privileges directly to
the pod's IAM DB user (`gke-rexi-sa@som-rit-phi-rexi-dev.iam`). The other two
tables (`rag_chunks`, `document_ingestion_state`) already exist.

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

### 3. Create the Secret (out-of-band — never commit values)
The pod needs two secret values as env vars. Create a plain Kubernetes Secret
(requires `container.secrets.create`, i.e. `roles/container.developer`):

```bash
kubectl -n rexi create secret generic rag-pipeline-secrets \
  --from-literal=AI_HUB_API_KEY="<aihub key>" \
  --from-literal=SHAREPOINT_SITE_REXI_CLIENT_SECRET="$(gcloud secrets versions access latest \
      --secret=SHAREPOINT_CLIENT_SECRET --project=som-rit-phi-redcap-prod)"
```

(The AI Hub key is the same one `rexi-app` uses — `ai.api.key` in its
`gke-app-sa-secrets`.)

**Long-term:** replace this raw Secret with the Secret Store CSI file mount like
`rexi-app`'s `secret-provider` SecretProviderClass. The container already reads
`/var/secrets/secret.properties` if mounted (see `rag_pipeline/utils/secret_file.py`),
so that swap is a manifest-only change — no image rebuild.

## Deploy (Flux GitOps — not `kubectl apply`)

The `rexi` namespace is reconciled by Flux from
[`susom/rexi-deploy`](https://github.com/susom/rexi-deploy/tree/main/som-rit-phi-rexi-dev).
The canonical CronJob + ConfigMap manifest lives there as `rag-pipeline.yaml`
(mirrors `configmap.yaml` + `cronjob.yaml` in this dir). To deploy: open a PR
adding/updating that file; on merge Flux applies it within ~10 min. The Secret
from step 3 is created out-of-band and referenced by name (`envFrom`), so no
secret values ever land in git.

The `configmap.yaml` / `cronjob.yaml` here are the source-of-truth reference for
what that Flux manifest should contain.

## First run — dry run before real ingest

Once the manifest is merged (Flux applied) and the Secret exists, de-risk by
exercising sourcing + dedup with **no writes** first. These `kubectl` commands
need `container.jobs.create` (`roles/container.developer`); if you only have
read access, ask someone who can create Jobs, or just let the nightly schedule
run and watch logs (`kubectl -n rexi logs job/<name> -f`, read-only is enough).

```bash
kubectl -n rexi create job rexi-dryrun --from=cronjob/rag-pipeline-rexi
# then edit the job command to add --dry-run, OR run a one-off:
kubectl -n rexi run rexi-dryrun --rm -it --restart=Never \
  --image=us-west1-docker.pkg.dev/som-rit-infrastructure-prod/rexi-rag-pipeline/rag-pipeline:latest \
  --serviceaccount=gke-rexi-sa \
  --overrides='{"spec":{"containers":[{"name":"x","image":"us-west1-docker.pkg.dev/som-rit-infrastructure-prod/rexi-rag-pipeline/rag-pipeline:latest","envFrom":[{"configMapRef":{"name":"rag-pipeline-config"}},{"secretRef":{"name":"rag-pipeline-secrets"}}],"command":["python","-m","rag_pipeline.ingest_batch","--site","rexi","--days-back","3650","--dry-run"]}]}}'
```

Check logs:
```bash
kubectl -n rexi logs job/<job-name> -f
```

Expected dry-run summary: approved docs detected, none ingested. Then trigger a
real run (`kubectl -n rexi create job rexi-manual --from=cronjob/rag-pipeline-rexi`)
and verify rows land:

```sql
SELECT count(*) FROM rexi.rag_chunks WHERE namespace = 'rexi_knowledge';
SELECT document_id, rag_ingestion_status, rag_last_ingested_at
FROM rexi.document_ingestion_state ORDER BY last_seen_at DESC LIMIT 20;
```

## Notes

- **Embedding model is fixed** at `text-embedding-3-small` (1536-dim) to match
  the vectors RExI queries `rag_chunks` with. Changing it requires re-embedding
  the whole table.
- **GitOps**: the `rexi` namespace is Flux-managed. The CronJob is deployed by
  committing `rag-pipeline.yaml` to `susom/rexi-deploy/som-rit-phi-rexi-dev/`
  (not `kubectl apply`). Flux reconciles the whole directory on merge to `main`.
- **Library drive IDs** in `configmap.yaml` cover the 8 named workflow libraries
  (Budget, Issuance of Award & Activation, Pre-startup, Prologue, Exploration,
  Regulatory, Startup, Contract). The default "Documents" and "TEST RExI"
  libraries are intentionally excluded.
