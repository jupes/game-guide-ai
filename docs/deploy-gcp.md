# Deploy runbook — GCP pilot hosting (`game-guide-ai-cloud`)

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
export PROJECT=game-guide-ai-cloud
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

**`--edition=ENTERPRISE` is required.** New Postgres 17 instances default to the
Enterprise **Plus** edition, which has no shared-core tiers — `db-f1-micro` only
exists on the Enterprise edition. (Enterprise Plus's cheapest tier alone exceeds
the $10 cap, so Enterprise is also the budget-correct choice.)

**Use a URL-safe password.** It gets embedded in the `DATABASE_URL` secret as a
URL DSN (`postgresql://postgres:PW@/...`) that the app parses at runtime, so a `:`,
`@`, `/`, `[` etc. will break both `psql` here and the deployed service. Generate a
pure-hex one — strong and safe everywhere: `export DBPW="$(openssl rand -hex 24)"`.

```bash
gcloud sql instances create game-guide-ai \
  --database-version=POSTGRES_17 --edition=ENTERPRISE --tier=db-f1-micro --region="$REGION" \
  --storage-size=10 --storage-type=HDD --availability-type=zonal
gcloud sql databases create game_guide_ai --instance=game-guide-ai
gcloud sql users set-password postgres --instance=game-guide-ai --password="$DBPW"

# Enable pgvector + create the schema. Via the Auth Proxy in one terminal:
#   cloud-sql-proxy "$PROJECT:$REGION:game-guide-ai" --port 6543
# then, in another, apply every init script in order (01 creates the vector
# extension + dnd schema; 02-04 add tables, indexes, hybrid search, chat schema):
PROXY="postgresql://postgres:<PW>@localhost:6543/game_guide_ai"
for f in 01-extensions.sql 02-schema.sql 03-hybrid-search.sql 04-chat-schema.sql; do
  psql "$PROXY" -f "vector-db/init/$f"
done
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
hard cap — it detaches billing at 100%. Run from the repo root — the function
deploy's `--source` is repo-relative.

> **`beta` track required for the budget.** `--all-updates-rule-pubsub-topic`
> (the Pub/Sub wiring the kill-switch depends on) is not on the GA
> `gcloud billing budgets create`; use `gcloud beta billing budgets create`.

```bash
gcloud pubsub topics create budget-alerts

gcloud beta billing budgets create \
  --billing-account="$BILLING_ACCOUNT_ID" \
  --display-name="game-guide-ai-cloud \$10" \
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

# Dump the dnd schema (corpus) from the CORRECT local DB (:5433):
pg_dump "postgresql://rag:rag_dev_change_me@localhost:5433/game_guide_ai" \
  -Fc --schema=dnd -f corpus-dnd.dump

# No local pg_dump? Use the running pg17 container's client and redirect stdout to
# a host file (exact server version; inside the container Postgres is on 5432, not
# the 5433 host mapping). Redirecting avoids an in-container path + docker cp — and
# on Windows Git Bash it also sidesteps MSYS path translation mangling `-f /tmp/...`:
#   docker exec game-guide-ai-vector-db pg_dump \
#     "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai" \
#     -Fc --schema=dnd > corpus-dnd.dump      # NB: no -t, it would corrupt the binary dump

# Restore DATA ONLY through the Auth Proxy (started in step 3, port 6543).
# The dnd.chunks table + indexes already exist (init/02 applied in step 3), so a
# full restore would collide on CREATE ("already exists"). --data-only loads just
# the 9,067 rows into the existing table.
#
# IMPORTANT: use a pg_restore at least as new as the pg17 pg_dump that wrote the
# archive, or you get "unsupported version (1.16) in file header". Cloud Shell's
# bundled client is older, so run the restore via the matching pg17 image
# (--network host lets it reach the proxy on 127.0.0.1:6543):
docker run --rm --network host -v ~/corpus-dnd.dump:/dump:ro pgvector/pgvector:pg17-bookworm \
  pg_restore --no-owner --data-only --dbname="$PROXY" /dump

# Verify with psql — version-independent, and needs no psycopg (Cloud Shell's
# system python lacks it, so `python vector-db/verify_db.py` would ModuleNotFound):
psql "$PROXY" -tAc "select count(*) from dnd.chunks;"   # → 9067
# Optional kNN smoke test (needs psycopg): uv run --with 'psycopg[binary]' --with pgvector \
#   env DATABASE_URL="$PROXY" PYTHONUTF8=1 python vector-db/verify_db.py
```

## 7. First locked deploy (Checkpoint D)

```bash
# Real deploy (preview first with --dry-run):
bash scripts/deploy.sh --dry-run
bash scripts/deploy.sh game-guide-ai "$(git rev-parse --short HEAD)"
```

> **Cloud Shell: `docker push` refused?** Pushes to `*-docker.pkg.dev` from Cloud
> Shell can fail at the TCP level (`dial tcp ...:443: connect: connection refused`)
> even with credentials correctly registered (hit 2026-07-25, twice, different
> Google IPs). Bypass Cloud Shell's push path by building server-side with Cloud
> Build, then run the `gcloud run deploy` step from `deploy.sh --dry-run` manually:
>
> ```bash
> IMAGE="us-central1-docker.pkg.dev/$PROJECT/game-guide-ai/game-guide-ai:$(git rev-parse --short HEAD)"
> cat > /tmp/cloudbuild.yaml <<EOF
> steps:
> - name: 'gcr.io/cloud-builders/docker'
>   args: ['build', '-f', 'Dockerfile.cloud', '-t', '${IMAGE}', '.']
> images: ['${IMAGE}']
> EOF
> gcloud builds submit --config /tmp/cloudbuild.yaml .
> ```
>
> (CI is unaffected — GitHub runners push over a different network path.)

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
| Secret | `GCP_DEPLOY_SA` | `gha-deployer@game-guide-ai-cloud.iam.gserviceaccount.com` |

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
