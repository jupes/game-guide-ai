# Deploy runbook — GCP pilot hosting (`game-guide-ai-pilot`)

Decision record + operator runbook for the closed pilot deployment (bead `x5bz.1`,
epic `17u`). Hosting decision (2026-07-22): **one Cloud Run service** (the
`Dockerfile.cloud` single-container UI+API image) backed by **Cloud SQL Postgres +
pgvector** (`db-f1-micro`, `us-central1`), a **$10/mo hard cap** enforced by a
billing kill-switch, and CI deploy via **Workload Identity Federation**.

> **Licensing lock.** The pilot serves a **closed** tester group on the full corpus
> (`x5bz.5`). Every deploy is `--no-allow-unauthenticated`. **Public ingress opens
> only after invite auth (`x5bz.2`) ships** — see [Open ingress](#9-open-ingress-deferred--x5bz16).
> `scripts/deploy.sh` and `tests/test_deploy_contract.py` enforce this in code.

The code side (Checkpoints A, B, and the kill-switch + CI wiring) is done and
tested. This runbook is the one-time infra bootstrap (Checkpoint C), the first
live deploy (Checkpoint D), and CI activation (Checkpoint E) — the steps that need
`gcloud` and the billing account.

## 0. Prerequisites

- `gcloud` CLI authenticated as an owner of the billing account (`gcloud auth login`).
- The billing account id: `gcloud billing accounts list` → `BILLING_ACCOUNT_ID`.
- The local corpus DB running on **port 5433** (db `game_guide_ai`) — **never 5432**,
  which is the legacy pre-rename corpus with corrupted PHB chunks.
- `docker` (for `deploy.sh`) and `pg_dump`/`pg_restore` (Postgres 17 client) locally.

```bash
export PROJECT=game-guide-ai-pilot
export REGION=us-central1
export BILLING_ACCOUNT_ID=XXXXXX-XXXXXX-XXXXXX   # from the list above
```

## 1. Project + APIs

```bash
gcloud projects create "$PROJECT"
gcloud billing projects link "$PROJECT" --billing-account="$BILLING_ACCOUNT_ID"
gcloud config set project "$PROJECT"
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbilling.googleapis.com pubsub.googleapis.com \
  cloudfunctions.googleapis.com cloudbuild.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com
```

## 2. Artifact Registry

```bash
gcloud artifacts repositories create game-guide-ai \
  --repository-format=docker --location="$REGION" \
  --description="game-guide-ai container images"
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
```

## 3. Cloud SQL — Postgres 17 + pgvector (`db-f1-micro`)

No authorized networks: nothing reaches the DB over its public IP; admin/migration
goes through the **Cloud SQL Auth Proxy** (IAM), and the app connects over the
Cloud Run socket (`--add-cloudsql-instances`).

```bash
gcloud sql instances create game-guide-ai \
  --database-version=POSTGRES_17 --tier=db-f1-micro --region="$REGION" \
  --storage-size=10 --storage-type=HDD --availability-type=zonal
gcloud sql databases create game_guide_ai --instance=game-guide-ai
gcloud sql users set-password postgres --instance=game-guide-ai --password="<CHOOSE_A_STRONG_PW>"

# Enable pgvector + create the schema. Via the Auth Proxy in one terminal:
#   cloud-sql-proxy "$PROJECT:$REGION:game-guide-ai" --port 6543
# then, in another (connection details from the proxy):
psql "postgresql://postgres:<PW>@localhost:6543/game_guide_ai" -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql "postgresql://postgres:<PW>@localhost:6543/game_guide_ai" -f vector-db/init/00_schema.sql   # + any other init/*.sql
```

The `INSTANCE_CONNECTION_NAME` is `"$PROJECT:$REGION:game-guide-ai"` — used by
`deploy.sh` (`CLOUDSQL_INSTANCE`) and the `DATABASE_URL` secret below.

## 4. Secret Manager

Secrets are injected by **reference** — `deploy.sh` never inlines a value.

```bash
printf '%s' "<YOUR_OPENAI_KEY>" | gcloud secrets create openai-api-key --data-file=-

# App DSN over the Cloud Run Cloud SQL socket (unix path form):
printf '%s' "postgresql://postgres:<PW>@/game_guide_ai?host=/cloudsql/$PROJECT:$REGION:game-guide-ai" \
  | gcloud secrets create database-url --data-file=-

# Let the Cloud Run runtime SA read them:
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
for s in openai-api-key database-url; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

## 5. $10 budget + Pub/Sub kill-switch

Alerts only notify; the Cloud Function (`scripts/gcp/billing_killswitch/`) is the
hard cap — it detaches billing at 100%.

```bash
gcloud pubsub topics create budget-alerts

gcloud billing budgets create \
  --billing-account="$BILLING_ACCOUNT_ID" \
  --display-name="game-guide-ai-pilot \$10" \
  --budget-amount=10USD \
  --filter-projects="projects/$PROJECT" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
  --all-updates-rule-pubsub-topic="projects/$PROJECT/topics/budget-alerts"

gcloud functions deploy billing-killswitch \
  --gen2 --runtime=python312 --region="$REGION" \
  --source=scripts/gcp/billing_killswitch \
  --entry-point=disable_billing_if_over_budget \
  --trigger-topic=budget-alerts \
  --set-env-vars="GCP_PROJECT=$PROJECT"

# The function's SA needs billing admin to detach billing:
KS_SA=$(gcloud functions describe billing-killswitch --gen2 --region="$REGION" --format='value(serviceConfig.serviceAccountEmail)')
gcloud billing accounts add-iam-policy-binding "$BILLING_ACCOUNT_ID" \
  --member="serviceAccount:$KS_SA" --role=roles/billing.admin
```

Verify: `gcloud billing budgets list --billing-account="$BILLING_ACCOUNT_ID"` shows the $10 budget.

## 6. Corpus migration (Checkpoint C, data)

Move the embedded corpus from local **:5433** into Cloud SQL — **no re-embedding**.
**Set `DATABASE_URL` explicitly**; `verify_db.py`'s fallback is `localhost:5432`
(the legacy corrupted corpus) and it does a sentinel insert+delete, not a read-only probe.

```bash
# Dump the dnd schema (corpus) from the CORRECT local DB (:5433):
pg_dump "postgresql://rag:rag_dev_change_me@localhost:5433/game_guide_ai" \
  -Fc --schema=dnd -f corpus-dnd.dump

# Restore through the Auth Proxy (started in step 3, port 6543):
pg_restore --no-owner --dbname="postgresql://postgres:<PW>@localhost:6543/game_guide_ai" corpus-dnd.dump

# Verify: row count matches local (9,067) and a kNN smoke query passes.
DATABASE_URL="postgresql://postgres:<PW>@localhost:6543/game_guide_ai" \
  PYTHONUTF8=1 python vector-db/verify_db.py
psql "postgresql://postgres:<PW>@localhost:6543/game_guide_ai" -tAc "select count(*) from dnd.chunks;"   # → 9067
```

## 7. First locked deploy (Checkpoint D)

```bash
# Real deploy (preview first with --dry-run):
bash scripts/deploy.sh --dry-run
bash scripts/deploy.sh game-guide-ai "$(git rev-parse --short HEAD)"
```

**Verify the lock and the app:**

```bash
SVC_URL=$(gcloud run services describe game-guide-ai --region="$REGION" --format='value(status.url)')
curl -s -o /dev/null -w '%{http_code}\n' "$SVC_URL/healthz"                              # → 403 (locked)
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$SVC_URL/healthz" # → {"status":"ok","ready":true}

# Full chat round-trip through the authenticated proxy (browser on localhost):
gcloud run services proxy game-guide-ai --region="$REGION"   # → http://localhost:8080
```

Ask a Sage-channel question in the proxied browser — a grounded answer with
citations from Cloud SQL confirms Checkpoint D.

**Optional — explicit `/healthz` startup probe.** Cloud Run's default startup probe
(TCP on `--port`) is sufficient for the pilot. For an HTTP readiness probe, export
the service, add the probe, and re-apply:

```yaml
# in `gcloud run services describe game-guide-ai --format=export > svc.yaml`, under the container:
startupProbe:
  httpGet: { path: /healthz, port: 8000 }
  periodSeconds: 5
  failureThreshold: 12
# then: gcloud run services replace svc.yaml
```

## 8. CI auto-deploy via Workload Identity Federation (Checkpoint E)

The `deploy` job in `.github/workflows/ci.yml` is already wired (WIF auth +
`id-token: write`). It stays dormant until these exist:

```bash
# Pool + provider bound to this GitHub repo:
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub"
gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --display-name="GitHub OIDC" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='jupes/game-guide-ai'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Deploy service account + roles (run admin, SA user, AR writer, SQL client):
gcloud iam service-accounts create gha-deployer --display-name="GitHub Actions deployer"
DEPLOYER="gha-deployer@${PROJECT}.iam.gserviceaccount.com"
for r in run.admin iam.serviceAccountUser artifactregistry.writer cloudsql.client; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$DEPLOYER" --role="roles/$r"
done
POOL=$(gcloud iam workload-identity-pools describe github --location=global --format='value(name)')
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/$POOL/attribute.repository/jupes/game-guide-ai"
```

Then set on the GitHub repo (Settings → Secrets and variables → Actions):

| Kind | Name | Value |
|------|------|-------|
| Variable | `DEPLOY_TARGET` | `game-guide-ai` |
| Secret | `GCP_WIF_PROVIDER` | the provider resource name (`.../providers/github`) |
| Secret | `GCP_DEPLOY_SA` | `gha-deployer@game-guide-ai-pilot.iam.gserviceaccount.com` |

Merge to `master` → the `deploy` job authenticates via WIF and runs `deploy.sh`.
Watch: `gh run watch` and `gcloud run revisions list --service game-guide-ai --region "$REGION"`.

## 9. Open ingress (DEFERRED — `x5bz.1.6`)

**Do not run this until invite auth (`x5bz.2`) has shipped.** Opening ingress
before app-level auth exposes the full-corpus app publicly and violates the
licensing posture (`x5bz.5`). When auth is live and verified:

```bash
gcloud run services update game-guide-ai --region="$REGION" --allow-unauthenticated
```

(That flag lives only here, never in `deploy.sh` — the guard test keeps it out.)

## Cost

~$9.4/mo steady state (Cloud SQL `db-f1-micro`), within the $10 cap. Corpus
migration is $0 (no re-embedding). Cloud Run scales to zero between testers.
