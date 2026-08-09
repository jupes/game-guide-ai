# Deploy runbook — GCP pilot hosting (`game-guide-ai-cloud`)

Decision record + operator runbook for the closed pilot deployment (bead `x5bz.1`,
epic `17u`). Hosting decision (2026-07-22): **one Cloud Run service** (the
`Dockerfile.cloud` single-container UI+API image) backed by **Cloud SQL Postgres +
pgvector** (`db-f1-micro`, `us-central1`), a **$10/mo hard cap** enforced by a
billing kill-switch, and CI deploy via **Workload Identity Federation**.

> **Licensing lock.** The pilot serves a **closed** tester group on the full corpus
> (`x5bz.5`). `scripts/deploy.sh` can never open ingress — the flag does not appear
> in it and `tests/test_deploy_contract.py` fails the build if it ever does.
> **Public ingress opens only after invite auth (`x5bz.2`) ships**, as a separate
> deliberate command — see [Open ingress](#9-open-ingress-deferred--x5bz16).
>
> Deploys default to `ACCESS=preserve`, which sends **no IAM flag at all** — not
> "figure out the current mode and re-send it", which would re-lock a live public
> service the moment a `describe` call hit a network blip or an expired
> credential. A new service is private anyway unless `allUsers` is granted. That
> default matters in both directions: a deploy must not open access, and once §9
> has opened it, a routine deploy must not silently take it away either.
> `ACCESS=locked` re-closes on purpose.

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
# then, in another. $PROXY is the operator DSN through that proxy; §6 and the
# §10/§11 incident sections all reuse it, so re-export it in every new shell:
export PROXY="postgresql://postgres:<PW>@localhost:6543/game_guide_ai"
scripts/bootstrap-db.sh "$PROXY"
```

The script applies every schema file in order and stops at the first failure
(exit 1). Do not skip it because the service re-applies the same DDL at startup:
that self-heals only once it boots, and minting the first invite with
`python -m service.admin_invites` needs `auth.users`/`auth.invites` to exist
before then.

The `INSTANCE_CONNECTION_NAME` is `"$PROJECT:$REGION:game-guide-ai"` — used by
`deploy.sh` (`CLOUDSQL_INSTANCE`) and the `DATABASE_URL` secret below.

## 4. Secret Manager

Secrets are injected by **reference** — `deploy.sh` never inlines a value.

```bash
printf '%s' "<YOUR_OPENAI_KEY>" | gcloud secrets create openai-api-key --data-file=-

# App DSN over the Cloud Run Cloud SQL socket (unix path form):
printf '%s' "postgresql://postgres:<PW>@/game_guide_ai?host=/cloudsql/$PROJECT:$REGION:game-guide-ai" \
  | gcloud secrets create database-url --data-file=-

# Session-cookie signing key (auth, x5bz.2). REQUIRED — without it the service
# fails closed: every auth endpoint 503s and no tester can log in. Generate it
# randomly; nobody needs to know or read this value:
openssl rand -base64 48 | tr -d '\n' | gcloud secrets create session-secret --data-file=-

# Let the Cloud Run runtime SA read them:
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
for s in openai-api-key database-url session-secret; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

**Rotating `session-secret` logs everyone out.** Sessions are stateless signed
cookies, so there is no server-side session list to revoke; adding a new secret
version and redeploying invalidates every outstanding session at once. That is
the intended lever if a session (or the secret) is ever suspected compromised:

```bash
openssl rand -base64 48 | tr -d '\n' | gcloud secrets versions add session-secret --data-file=-
bash scripts/deploy.sh game-guide-ai "$(git rev-parse --short HEAD)"   # picks up :latest
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

```bash
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
# $PROXY is set in step 3 — re-export it if this is a fresh shell. Unset, it is
# an empty DSN, and pg_restore loads 9,067 rows into a LOCAL database instead.
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

# Lock check: an anonymous request must NOT get 200. A private Cloud Run service
# returns Google's 403/404 to unauthenticated callers — either proves it's locked:
curl -s -o /dev/null -w '%{http_code}\n' "$SVC_URL/healthz"       # → 403 or 404 (locked ✓)
```

> **Human operators: verify via the proxy, not a raw identity-token curl.**
> `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)"` works from a
> **service account** (what CI uses in step 8), but a **user** account mints a token
> whose audience doesn't match the service URL, so Cloud Run rejects it (you get the
> same 403/404). Use `gcloud run services proxy`, which authenticates correctly:

```bash
# Full chat round-trip through the authenticated proxy:
gcloud run services proxy game-guide-ai --region="$REGION"   # → http://127.0.0.1:8080
# In Cloud Shell: Web Preview → port 8080. Append /healthz for {"status":"ok","ready":true}.
```

Ask a Sage-channel question in the proxied browser — a grounded answer with
citations from Cloud SQL confirms Checkpoint D. Cold start builds the corpus
vocabulary first, so the first `/healthz` may read `ready:false` for ~20-30s.

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
gcloud run services add-iam-policy-binding game-guide-ai \
  --region="$REGION" --member=allUsers --role=roles/run.invoker

# Confirm — the same check §10 step 1 uses:
gcloud run services get-iam-policy game-guide-ai --region "$REGION" \
  --format='value(bindings.members)' | grep -q allUsers \
  && echo "public invoke intact" || echo "still IAM-LOCKED"
```

Opening ingress is an **IAM binding**, not a service setting.
`--allow-unauthenticated` is sugar on `gcloud run deploy`, which expands to this
same binding; `gcloud run services update` rejects it outright
(`unrecognized arguments`). Granting `roles/run.invoker` to `allUsers` is the
form that works against an already-deployed service, and it is what makes the
Google front end stop returning 403 before a request ever reaches the app.

Public *invoke* is not public *access*: every endpoint still requires a session,
which is the whole point of gating this behind `x5bz.2`. `deploy.sh` never
requests either form — the guard test keeps `--allow-unauthenticated` out of it.

**It stays open across later deploys.** `deploy.sh` defaults to
`ACCESS=preserve` and passes no IAM flag at all, so shipping code — or the
incident-response redeploy in §10 — leaves this binding
untouched. It used to hardcode `--no-allow-unauthenticated`, which would have
quietly re-locked the service on the next CI push and handed every tester an
edge 403 with no login page behind it. `preserve` deliberately does **not** read
the current mode back first: every way that read can fail (transient API error,
expired credential, missing permission) looks identical to "no such service",
and acting on that would re-lock a live public service. To re-close
deliberately: `ACCESS=locked bash scripts/deploy.sh …`.

### Before you run it: the ingress + proxy-hop pair

Opening ingress makes `/auth/login` reachable by anyone, so its rate limiting has
to be keying on something real. Two settings have to agree, and getting either
wrong is silent:

1. **Ingress stays restricted to Google's front end** — `--ingress all` means
   "all *via the run.app front end*", which is what we want; it must **not** be
   `internal` (testers can't reach it) and the container must not be reachable by
   any path that bypasses that front end. A caller who can reach the container
   directly becomes the "trusted" hop and can write the whole
   `X-Forwarded-For` chain themselves.
2. **`AUTH_TRUSTED_PROXY_HOPS` matches the topology** — `deploy.sh` sets `1`, the
   number of entries Cloud Run's default front end appends. Change it to `2` if
   an external HTTPS load balancer is ever put in front, and back to `0` for any
   deployment with nothing in front. Too high and the limiter starts trusting
   caller-supplied entries (an attacker rotates them for unlimited budget); left
   at `0` behind a proxy, every tester shares one bucket.

### Verify it — behaviorally, not by reading a log

A request log only shows what **Google** observed (`httpRequest.remoteIp`); it
never shows the value `client_source()` picked out of the `X-Forwarded-For`
chain, and hop counts of 0, 1 or a badly wrong value all produce
identical-looking entries. The check has to be behavioral:

```bash
SVC_URL=$(gcloud run services describe game-guide-ai --region="$REGION" --format='value(status.url)')
python scripts/verify_auth_throttle.py "$SVC_URL"
```

It sends attempts that differ **only** in a spoofed `X-Forwarded-For` (rotating
the email too, so the account limiter can't be what trips) and reports through
its exit status:

| Exit | Verdict | Meaning |
|---|---|---|
| `0` | **PASS** | A 429 carrying the app's `X-Auth-Throttled` marker. The spoofed values shared one budget, so the key is not caller-controlled |
| `1` | **FAIL** | The whole budget spent, all 401s. Each spoof bought its own budget — the hop count reaches past the trusted entries into caller-written text, or ingress can be bypassed |
| `2` | REFUSE | The attempt count can't exhaust the derived budget, so a FAIL would be meaningless. Nothing is sent |
| `3` | ABORT | Something unexplained (403, 422, 5xx, or a 429 **without** the marker). Inconclusive — never a pass |

The marker is why a bare 429 isn't enough: **Cloud Run returns 429 itself when
no instance is available**, which is indistinguishable by status code, so
accepting any 429 would certify the proxy configuration on the strength of a
transient platform response. Only the header the app sets counts, and an
unmarked 429 aborts. (`tests/test_throttle_verifier.py` pins all of these.)

The run spends your own source budget for `AUTH_RATE_LIMIT_WINDOW_S` (5 min) and
then decays; nothing to clear.

That test can't tell hops `0` from hops `1` — both resist spoofing, but `0`
collapses every tester into one shared bucket. For that half, compare the key the
service *derived* against the address Google *observed*.

Those are two different log entries: the throttle line is an application log
(`jsonPayload`), while `httpRequest.remoteIp` belongs to Cloud Run's separate
request log. A single query over one of them cannot show both — join them by
**trace**, which the service now emits on the structured entry:

```bash
# 1. the derived key + the trace of the request it came from
gcloud logging read \
  'resource.type="cloud_run_revision" AND jsonPayload.message="auth attempt throttled"' \
  --project "$PROJECT" --limit 1 --format='value(jsonPayload.source, trace)'
# -> 203.0.113.7   projects/<PROJECT>/traces/<TRACE_ID>

# 2. the request entry for that same trace
gcloud logging read \
  'logName:"run.googleapis.com%2Frequests" AND trace="projects/'"$PROJECT"'/traces/<TRACE_ID>"' \
  --project "$PROJECT" --limit 1 --format='value(httpRequest.remoteIp)'
# -> 203.0.113.7
```

The two must be equal. If they differ, `AUTH_TRUSTED_PROXY_HOPS` is reading the
wrong position in the chain. If entries from **different** testers' networks all
report the same `jsonPayload.source`, the hop count is too low and everyone is
sharing one budget.

(The trace field needs `GCP_PROJECT`, which `deploy.sh` sets. Without it the
entry still carries a bare trace id — searchable, just not auto-correlated.)

## 10. Incident — a leaked invite or a compromised account

**Set the shell up first.** Every command below reads `$PROXY`, and the failure
is silent: unset, it is an empty DSN, so `psql` connects to whatever local
database it finds and `admin_invites` falls back to the local development DSN.
Both then report success — against the wrong database, with production access
left exactly where it was.

```bash
export PROJECT=game-guide-ai-cloud REGION=us-central1
cloud-sql-proxy "$PROJECT:$REGION:game-guide-ai" --port 6543 &   # if not already up

export PROXY="postgresql://postgres:<PW>@localhost:6543/game_guide_ai"   # as in §3
export DATABASE_URL="$PROXY"    # what `python -m service.admin_invites` reads
export SVC_URL="$(gcloud run services describe game-guide-ai \
  --region="$REGION" --format='value(status.url)')"

# Prove you are on Cloud SQL before touching anything:
psql "$PROXY" -tAc 'select current_user, current_database();'   # → postgres|game_guide_ai
```

`postgres` is the discriminator — the local development DSN connects as `rag`.
Run `admin_invites` from the repo root.

An invite that has **not** been redeemed — revoke it and you are done:

```bash
python -m service.admin_invites revoke <token>
```

If it *was* redeemed, the account exists, and the order below is load-bearing.
`require_session` validates at the **start** of a request, so a request already
admitted keeps its authorization for as long as it runs — up to Cloud Run's
`--timeout` (300s). Deleting first leaves exactly that window for an in-flight
`/chat` or upload to write rows after the cleanup.

**1. Cut off access.** Rotate the signing secret and redeploy; every session
cookie becomes unverifiable, so no *new* request can authenticate.

```bash
openssl rand -base64 48 | tr -d '\n' | gcloud secrets versions add session-secret --data-file=-
bash scripts/deploy.sh game-guide-ai "$(git rev-parse --short HEAD)"
```

This logs **everyone** out — stateless cookies have no server-side revocation —
so the other testers must still be able to reach the login screen. `deploy.sh`
defaults to `ACCESS=preserve` and leaves the IAM mode alone precisely so this
mid-incident redeploy cannot also revoke invocation and turn every session into
an edge 403 with no login page behind it. Confirm:

```bash
gcloud run services get-iam-policy game-guide-ai --region "$REGION" \
  --format='value(bindings.members)' | grep -q allUsers \
  && echo "public invoke intact" \
  || echo "IAM-LOCKED — testers cannot reach the login page"
```

(Before §9 opens ingress the service *is* IAM-locked, and that second line is
the correct state.)

**2. Drain.** Traffic allocation is the signal, not readiness: a retired
revision normally stays `Ready` at 0%, so one still taking every request looks
identical to one taking none.

```bash
gcloud run services describe game-guide-ai --region "$REGION" \
  --format='value(status.traffic[].revisionName, status.traffic[].percent)'
```

Wait for the new revision at 100% and the old at 0 (or gone) **before** starting
the clock — requests the old revision already admitted may still be running.

```bash
sleep 360   # > --timeout, counted from the 100% cutover, not from the deploy
```

**3. Delete.** Now nothing can write on the account's behalf.

```bash
psql "$PROXY" -c "DELETE FROM auth.users WHERE lower(email) = lower('them@example.com');"
```

One statement suffices: `chat.conversations.user_id` references `auth.users`
`ON DELETE CASCADE`, and messages/attachments cascade from conversations, so the
content goes too. `auth.invites.used_by` is `ON DELETE SET NULL` — the invite row
survives as the audit trail that its token was spent, it just forgets who.

Those keys are also the backstop for step 1: a write that slips through anyway is
rejected by the database rather than silently recreating an ownership row for a
user id that no longer exists. Verify:

```bash
psql "$PROXY" -c "SELECT count(*) FROM chat.conversations c
                    LEFT JOIN auth.users u ON u.id = c.user_id
                   WHERE u.id IS NULL;"   -- expect 0
```

Conversations predating the ownership table are the one exception the cascade
cannot reach (their messages have no owner row, which is why the constraint is
`NOT VALID`). That is pre-auth data, not this account's.

## 11. A tester forgot their password

**There is no password reset.** It needs outbound email, which the pilot does
not have, and there is no admin reset command. Minting a second invite does not
help on its own: signup rejects an email that already has an account
(`EmailTaken`, 409). The invite remains unused, but it cannot create a second
account with that email until the old account is deleted.

Recovery is **delete the account, then re-invite** — and it is destructive. Set
the shell up as in §10 first; `$SVC_URL` matters here because `--base-url`
otherwise defaults to `http://localhost:8000` and mints a link to nowhere.

```bash
# 1. Delete. This CASCADES: their conversations, messages and attachments go too.
psql "$PROXY" -c "DELETE FROM auth.users WHERE lower(email) = lower('them@example.com');"

# 2. Mint a fresh invite for the same person (--role dm to restore GM access).
python -m service.admin_invites create --role player --base-url "$SVC_URL"
```

They sign up again with the same email — now unused — and start with empty
history. Tell them that before you do it.

No secret rotation and no drain here: this is a cooperative user, not an
adversary with a live session, so there is nothing to race. If you are *not*
sure the account is uncompromised, treat it as §10 instead.

## Cost

~$9.4/mo steady state (Cloud SQL `db-f1-micro`), within the $10 cap. Corpus
migration is $0 (no re-embedding). Cloud Run scales to zero between testers.
