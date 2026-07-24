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
# The pilot serves a CLOSED tester group (x5bz.5). This script always deploys
# with --no-allow-unauthenticated and never opens public ingress. That opens only
# after invite auth (x5bz.2) lands — tracked by bead x5bz.1.6 and the "Open
# ingress" section of docs/deploy-gcp.md. A repo guard test
# (tests/test_deploy_contract.py) fails the build if the bare public-ingress flag
# ever appears here again, so the lock cannot silently regress.
set -euo pipefail

# ── Config (env-overridable; real values live in CI vars / the operator shell) ─
REGION="${GCP_REGION:-us-central1}"
PROJECT="${GCP_PROJECT:-game-guide-ai-pilot}"
AR_REPO="${AR_REPO:-game-guide-ai}"                       # Artifact Registry repo
CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:-${PROJECT}:${REGION}:game-guide-ai}"
# Secret Manager secret NAMES — values are never inlined here.
OPENAI_SECRET="${OPENAI_SECRET:-openai-api-key}"
DATABASE_URL_SECRET="${DATABASE_URL_SECRET:-database-url}"

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

echo "Deploy plan: service=${SERVICE} sha=${SHA}"
echo "  image=${IMAGE}"
if [ "$DRY_RUN" = "1" ]; then
  echo "  (dry-run: printing commands, executing nothing)"
fi

# 1. Build the single-container image (Dockerfile.cloud). Cloud Run is linux/amd64.
run docker build --platform linux/amd64 -f Dockerfile.cloud -t "${IMAGE}" .

# 2. Push to Artifact Registry (operator/CI has run `gcloud auth configure-docker`).
run docker push "${IMAGE}"

# 3. Deploy to Cloud Run — LOCKED. Cloud SQL attached by socket; OPENAI_API_KEY
#    and DATABASE_URL injected by Secret Manager reference (never values); the
#    app listens on 8000 (Cloud Run defaults to 8080, so --port is required).
#    The /healthz startup probe is set via the service YAML in docs/deploy-gcp.md
#    (kept out of this flag list so an unsupported gcloud flag can't break deploy).
run gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --port 8000 \
  --no-allow-unauthenticated \
  --add-cloudsql-instances "${CLOUDSQL_INSTANCE}" \
  --set-secrets "OPENAI_API_KEY=${OPENAI_SECRET}:latest,DATABASE_URL=${DATABASE_URL_SECRET}:latest" \
  --timeout 300 \
  --max-instances 2

echo "Done."
