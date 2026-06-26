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
`rexi.ingestion_locks`, grants schema/table/sequence privileges to `rexi_app`,
and grants `rexi_app` to the pod's IAM DB user. The other two tables
(`rag_chunks`, `document_ingestion_state`) already exist.

> If inserts later fail with "permission denied for table ...", the IAM role
> isn't inheriting `rexi_app` — confirm `GRANT rexi_app TO "gke-rexi-sa@som-rit-phi-rexi-dev.iam"`
> ran and the role has `INHERIT`.

### 2. Build + push the image (needs Artifact Registry write)
`irvins@stanford.edu` lacks `artifactregistry.repositories.create/push` in
`som-rit-phi-rexi-dev`, so this step needs someone with AR write (or the
existing `som-rit-infrastructure-prod/rexi` CI). The image is plain amd64 and
builds from the repo root (verified):

```bash
# Pick a registry the rexi-cluster node SA can pull from. Two options:
#   A) a new repo in rexi-dev (preferred — same project as the cluster), or
#   B) the existing som-rit-infrastructure-prod/rexi repo used by rexi-app.

# Option A — create the repo once (needs artifactregistry.admin on rexi-dev):
gcloud artifacts repositories create rag-pipeline \
  --repository-format=docker --location=us-west1 \
  --project=som-rit-phi-rexi-dev

REPO=us-west1-docker.pkg.dev/som-rit-phi-rexi-dev/rag-pipeline/ingest
gcloud auth configure-docker us-west1-docker.pkg.dev

# Build amd64 (GKE nodes are amd64) and push:
docker buildx build --platform linux/amd64 -t "$REPO:$(git rev-parse --short HEAD)" -t "$REPO:latest" --push .
```

Then set `spec.jobTemplate...containers[0].image` in `cronjob.yaml` to the
pushed tag. If you use Option A, also grant the node SA read:

```bash
gcloud artifacts repositories add-iam-policy-binding rag-pipeline \
  --location=us-west1 --project=som-rit-phi-rexi-dev \
  --member="serviceAccount:rexi-gke-nodes-sa@som-rit-phi-rexi-dev.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

### 3. Create the Secret (don't commit values)
```bash
kubectl -n rexi create secret generic rag-pipeline-secrets \
  --from-literal=AI_HUB_API_KEY="<aihub key>" \
  --from-literal=SHAREPOINT_SITE_REXI_CLIENT_SECRET="$(gcloud secrets versions access latest \
      --secret=SHAREPOINT_CLIENT_SECRET --project=som-rit-phi-redcap-prod)"
```
(The AI Hub key is the same one `rexi-app` uses — `ai.api.key` in its
`gke-app-sa-secrets`.) Preferably wire both through the Secret Store CSI driver
like the `rexi-app` `secret-provider` SecretProviderClass instead of a raw Secret.

## Apply

```bash
kubectl apply -f deploy/gke/configmap.yaml
# (Secret created in step 3 above)
kubectl apply -f deploy/gke/cronjob.yaml
```

## First run — dry run before real ingest

De-risk by exercising sourcing + dedup with **no writes** first:

```bash
kubectl -n rexi create job rexi-dryrun --from=cronjob/rag-pipeline-rexi
# then edit the job command to add --dry-run, OR run a one-off:
kubectl -n rexi run rexi-dryrun --rm -it --restart=Never \
  --image=<your image> --serviceaccount=gke-rexi-sa \
  --overrides='{"spec":{"containers":[{"name":"x","image":"<your image>","envFrom":[{"configMapRef":{"name":"rag-pipeline-config"}},{"secretRef":{"name":"rag-pipeline-secrets"}}],"command":["python","-m","rag_pipeline.ingest_batch","--site","rexi","--days-back","3650","--dry-run"]}]}}'
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
- **GitOps**: the `rexi` namespace is Flux-managed. If you want this CronJob
  reconciled by Flux, add these manifests to the Flux source repo instead of
  `kubectl apply`.
- **Library drive IDs** in `configmap.yaml` cover the 8 named workflow libraries
  (Budget, Issuance of Award & Activation, Pre-startup, Prologue, Exploration,
  Regulatory, Startup, Contract). The default "Documents" and "TEST RExI"
  libraries are intentionally excluded.
