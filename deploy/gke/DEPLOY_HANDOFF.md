# RExI pipeline — deploy handoff (one-time)

Ordered, copy-paste runbook to get the RExI content pipeline running as a GKE
CronJob. Requires **Artifact Registry write** + **`roles/container.developer`**
(or equivalent edit) on project `som-rit-phi-rexi-dev`.

Context:
- Cluster: `rexi-cluster` (GKE Autopilot, region `us-west1`, project `som-rit-phi-rexi-dev`)
- Namespace: `rexi` (same as the RExI app)
- The CronJob runs `python -m rag_pipeline.ingest_batch --site rexi --days-back 1`
  as the `gke-rexi-sa` Workload Identity SA, writing to `rexi.rag_chunks`.
- DB readiness SQL has already been run (✅ `ingestion_locks` + grants exist).

All commands are run from the **content_pipeline repo root** unless noted.

---

## 0. Prereqs (once per machine)

```bash
gcloud auth login
gcloud config set project som-rit-phi-rexi-dev
gcloud container clusters get-credentials rexi-cluster --region=us-west1 --project=som-rit-phi-rexi-dev
```

---

## 1. Build + push the image (needs AR write)

```bash
# 1a. Create the Artifact Registry repo (once; needs artifactregistry.admin):
gcloud artifacts repositories create rag-pipeline \
  --repository-format=docker --location=us-west1 \
  --project=som-rit-phi-rexi-dev \
  --description="RAG content pipeline images"

# 1b. Let the cluster's node SA pull from it:
gcloud artifacts repositories add-iam-policy-binding rag-pipeline \
  --location=us-west1 --project=som-rit-phi-rexi-dev \
  --member="serviceAccount:rexi-gke-nodes-sa@som-rit-phi-rexi-dev.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader

# 1c. Build amd64 (GKE nodes are amd64) and push:
gcloud auth configure-docker us-west1-docker.pkg.dev
REPO=us-west1-docker.pkg.dev/som-rit-phi-rexi-dev/rag-pipeline/ingest
docker buildx build --platform linux/amd64 -t "$REPO:latest" -t "$REPO:$(git rev-parse --short HEAD)" --push .
```

`deploy/gke/cronjob.yaml` already points at `$REPO:latest`, so no edit needed if
you use this repo path. (If you push elsewhere, update the `image:` field.)

---

## 2. Create the Secret (needs container.developer)

The AI Hub key is the same one the RExI app uses (`ai.api.key`). The SharePoint
client secret is pulled live from Secret Manager:

```bash
kubectl -n rexi create secret generic rag-pipeline-secrets \
  --from-literal=AI_HUB_API_KEY="<paste AI Hub api key>" \
  --from-literal=SHAREPOINT_SITE_REXI_CLIENT_SECRET="$(gcloud secrets versions access latest \
      --secret=SHAREPOINT_CLIENT_SECRET --project=som-rit-phi-redcap-prod)"
```

---

## 3. Apply the config + CronJob

```bash
kubectl apply -f deploy/gke/configmap.yaml
kubectl apply -f deploy/gke/cronjob.yaml
kubectl -n rexi get cronjob rag-pipeline-rexi
```

---

## 4. DRY RUN first (sources + dedups, writes NOTHING)

Run a one-off Job with the command overridden to `--dry-run` and a wide date
window so it sees all approved docs:

```bash
kubectl -n rexi apply -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata:
  name: rexi-dryrun
  namespace: rexi
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 3600
  template:
    spec:
      restartPolicy: Never
      serviceAccountName: gke-rexi-sa
      tolerations:
        - { effect: NoSchedule, key: kubernetes.io/arch, operator: Equal, value: amd64 }
      containers:
        - name: ingest
          image: us-west1-docker.pkg.dev/som-rit-phi-rexi-dev/rag-pipeline/ingest:latest
          command: ["python","-m","rag_pipeline.ingest_batch","--site","rexi","--days-back","3650","--dry-run"]
          envFrom:
            - configMapRef: { name: rag-pipeline-config }
            - secretRef:    { name: rag-pipeline-secrets }
          volumeMounts:
            - { name: cache, mountPath: /app/cache }
            - { name: tmp,   mountPath: /tmp }
      volumes:
        - { name: cache, emptyDir: {} }
        - { name: tmp,   emptyDir: {} }
YAML

# Watch it:
kubectl -n rexi wait --for=condition=complete job/rexi-dryrun --timeout=1800s &
kubectl -n rexi logs -f job/rexi-dryrun
```

**Expect:** a JSON summary showing approved docs detected, `dry_run: true`, and
**0 sections ingested**. If you see auth/connection errors, stop and check
section 7 below before doing a real run.

Clean up: `kubectl -n rexi delete job rexi-dryrun`

---

## 5. Real manual run

```bash
kubectl -n rexi create job rexi-manual --from=cronjob/rag-pipeline-rexi
kubectl -n rexi logs -f job/rexi-manual
```

**Expect:** JSON summary with `documents_processed > 0` and `sections_ingested > 0`
(on the first run; later runs skip unchanged docs via content-hash dedup).

---

## 6. Verify in the database (Cloud SQL Studio, `rexi_db`)

```sql
-- Chunks written by the pipeline:
SELECT count(*) FROM rexi.rag_chunks WHERE namespace = 'rexi_knowledge';

-- Per-document ingestion state (dedup/version ledger):
SELECT document_id, rag_ingestion_status, sections_processed, sections_total,
       rag_last_ingested_at
FROM rexi.document_ingestion_state
ORDER BY last_seen_at DESC
LIMIT 20;
```

Also check the SharePoint **"RExI Content Status List"** — successfully ingested
docs get a tracker row ("Ingested successfully").

After this, the CronJob runs automatically every night at 09:00 UTC.

---

## 7. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ImagePullBackOff` | Node SA can't pull — re-run step 1b, or wrong `image:` path. |
| `permission denied for table ...` | Re-run `deploy/gke/db_readiness.sql` (grants to `gke-rexi-sa`). |
| DB connection / auth timeout | Confirm pod is in ns `rexi` with `serviceAccountName: gke-rexi-sa`; `rexi.db.internal` only resolves in-cluster. |
| `type "vector" does not exist` | pgvector extension/search_path — already handled in code (`search_path = rexi, public`); ensure `DB_SCHEMA=rexi`. |
| AI Hub 401/403 | `AI_HUB_API_KEY` secret value wrong/missing. |
| SharePoint 401 | `SHAREPOINT_SITE_REXI_CLIENT_SECRET` wrong, or app lost access to the RExI site. |
| Job hangs | `activeDeadlineSeconds: 3600` caps it; check logs for the stuck doc. |

Useful:
```bash
kubectl -n rexi get jobs
kubectl -n rexi get pods -l app=rag-pipeline
kubectl -n rexi describe job/<name>
kubectl -n rexi logs job/<name>
```
