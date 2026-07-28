#!/usr/bin/env bash
#
# Cloud Run deploy entrypoint for the game-guide-ai pilot (x5bz.1 Checkpoint B).
#
# Invoked by CI (ci.yml deploy job) as:
#     ./scripts/deploy.sh "$DEPLOY_TARGET" "$GITHUB_SHA"
#   $1 = deploy target  — Cloud Run service name (e.g. game-guide-ai)
#   $2 = commit SHA      — image tag (CI passes $GITHUB_SHA)
# Locally, preview without touching anything:
#     bash scripts/deploy.sh --dry-run     # prints the plan, runs nothing
#
# ── LICENSING LOCK ────────────────────────────────────────────────────────────
# The pilot serves a CLOSED tester group (x5bz.5). This script NEVER opens public
# ingress: --allow-unauthenticated does not appear here at all, and a repo guard
# test (tests/test_deploy_contract.py) fails the build if it ever does. Opening
# is a separate, deliberate `gcloud run services update` — bead x5bz.1.6 and the
# "Open ingress" section of docs/deploy-gcp.md.
#
# It does not *close* ingress either, unless asked. The IAM mode is an explicit
# input (ACCESS, below) rather than a hardcoded flag, because hardcoding
# --no-allow-unauthenticated meant every later deploy — a routine CI push, or the
# incident-response redeploy in docs/invite-copy.md — silently revoked tester
# access after x5bz.1.6, handing them Cloud Run IAM 403s at the edge with no
# sign-in page to explain it.
set -euo pipefail

# ── Config (env-overridable; real values live in CI vars / the operator shell) ─
REGION="${GCP_REGION:-us-central1}"
PROJECT="${GCP_PROJECT:-game-guide-ai-cloud}"
AR_REPO="${AR_REPO:-game-guide-ai}"                       # Artifact Registry repo
CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:-${PROJECT}:${REGION}:game-guide-ai}"
# Secret Manager secret NAMES — values are never inlined here.
OPENAI_SECRET="${OPENAI_SECRET:-openai-api-key}"
DATABASE_URL_SECRET="${DATABASE_URL_SECRET:-database-url}"
# Signs the auth session cookie (x5bz.2). REQUIRED: the service fails closed
# (503 on every auth endpoint) rather than signing with an empty key, so a
# deploy without this secret has no working login. Rotating it invalidates
# every live session — that is the intended "log everyone out" lever.
SESSION_SECRET_SECRET="${SESSION_SECRET_SECRET:-session-secret}"

# Who may INVOKE the service (Cloud Run IAM), independent of the app's own auth:
#   preserve (default) — pass no IAM flag, so an existing service keeps whatever
#                        mode it is in. A service that does not exist yet is
#                        CREATED LOCKED: preserve must never mean "open".
#   locked             — force --no-allow-unauthenticated (pre-x5bz.1.6 posture,
#                        or to re-close a service deliberately).
# There is no "public" value: opening ingress stays a separate, explicit command
# (docs/deploy-gcp.md §9) so it can never be a side effect of shipping code.
ACCESS="${ACCESS:-preserve}"
case "$ACCESS" in
  preserve|locked) ;;
  *) echo "ACCESS must be 'preserve' or 'locked' (got '${ACCESS}')" >&2; exit 2 ;;
esac

# ── Args ──────────────────────────────────────────────────────────────────────
DRY_RUN=0
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done
SERVICE="${POSITIONAL[0]:-game-guide-ai}"
SHA="${POSITIONAL[1]:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${SERVICE}:${SHA}"

# In dry-run, print each command indented; otherwise execute it.
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '  %s\n' "$*"
  else
    "$@"
  fi
}

# Resolve ACCESS into the actual gcloud flags.
#
# `preserve` passes NOTHING, unconditionally. It does NOT probe whether the
# service exists first: `gcloud run services describe` fails for a transient
# network blip, an expired credential or a missing permission exactly as it does
# for a service that isn't there, and treating all of those as "doesn't exist"
# would send --no-allow-unauthenticated at a live, public service — re-locking it
# out from under the testers, with the build+push window giving the underlying
# problem time to clear so the deploy still "succeeds". A mode called `preserve`
# must have no path that changes IAM.
#
# Passing nothing is safe for a first deploy too: Cloud Run services are private
# unless allUsers is granted the invoker role, and --quiet keeps a
# non-interactive create from stopping on the "allow unauthenticated?" prompt
# (whose non-interactive default is no).
IAM_FLAGS=()
if [ "$ACCESS" = "locked" ]; then
  IAM_FLAGS=(--no-allow-unauthenticated)
  ACCESS_NOTE="locked (forcing --no-allow-unauthenticated)"
else
  ACCESS_NOTE="preserve (no IAM flag sent — live policy untouched; a NEW service is private by default)"
fi

echo "Deploy plan: service=${SERVICE} sha=${SHA}"
echo "  image=${IMAGE}"
echo "  access=${ACCESS_NOTE}"
if [ "$DRY_RUN" = "1" ]; then
  echo "  (dry-run: printing commands, executing nothing)"
fi

# 1. Build the single-container image (Dockerfile.cloud). Cloud Run is linux/amd64.
run docker build --platform linux/amd64 -f Dockerfile.cloud -t "${IMAGE}" .

# 2. Push to Artifact Registry (operator/CI has run `gcloud auth configure-docker`).
run docker push "${IMAGE}"

# 3. Deploy to Cloud Run. Cloud SQL attached by socket; OPENAI_API_KEY
#    and DATABASE_URL injected by Secret Manager reference (never values); the
#    app listens on 8000 (Cloud Run defaults to 8080, so --port is required).
#    --memory / --concurrency are set EXPLICITLY, not left to the platform
#    defaults (512 MiB / 80 concurrent): /auth/login runs a 64 MiB argon2 hash on
#    every attempt, so those defaults would let a handful of simultaneous logins
#    push the instance over its memory limit and get it killed. 1 GiB comfortably
#    covers the app plus MAX_CONCURRENT_HASHES * ARGON2_MEMORY_KIB (2 * 64 MiB);
#    keep the three in sync (see config.py / service/hashing.py).
#    The /healthz startup probe is set via the service YAML in docs/deploy-gcp.md
#    (kept out of this flag list so an unsupported gcloud flag can't break deploy).
#    AUTH_TRUSTED_PROXY_HOPS=1: on the default run.app front end exactly one
#    trusted entry is appended to X-Forwarded-For, and the auth rate limiter keys
#    on THAT entry — everything to its left is caller-written and ignored. If an
#    external HTTPS load balancer is ever put in front, this becomes 2. It is only
#    sound while ingress is restricted to that front end (see docs/deploy-gcp.md);
#    a caller who can reach the container directly is the trusted hop.
#    GCP_PROJECT is what lets the app emit a full Cloud Trace resource name on its
#    structured logs, so a container log line can be joined to its request log
#    entry (docs/deploy-gcp.md §9 verification). Not a secret.
run gcloud run deploy "${SERVICE}" \
  --quiet \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --port 8000 \
  ${IAM_FLAGS[@]+"${IAM_FLAGS[@]}"} \
  --add-cloudsql-instances "${CLOUDSQL_INSTANCE}" \
  --set-secrets "OPENAI_API_KEY=${OPENAI_SECRET}:latest,DATABASE_URL=${DATABASE_URL_SECRET}:latest,SESSION_SECRET=${SESSION_SECRET_SECRET}:latest" \
  --set-env-vars "AUTH_TRUSTED_PROXY_HOPS=1,GCP_PROJECT=${PROJECT}" \
  --timeout 300 \
  --max-instances 2 \
  --memory 1Gi \
  --concurrency 20

echo "Done."
