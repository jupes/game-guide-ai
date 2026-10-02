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

# ── Sign in with Google (lvs7) ─────────────────────────────────────────────────
# OPTIONAL, and OFF unless GOOGLE_OAUTH_CLIENT_ID is set (a CI repository
# VARIABLE, not a secret: the id is in the URL the browser visits). Off means
# the service answers 404 on every /auth/google route and the UI draws no button.
#
# It is carried HERE, in the same --set-env-vars / --set-secrets flags as
# everything else, because both flags REPLACE the service's whole set on every
# deploy (the 1kg.9.5 release-review finding): a value set with
# `gcloud run services update` alone is wiped by the next CI deploy.
#
# The client SECRET is never a variable of this script. It lives in Secret
# Manager and reaches the service as a --set-secrets reference, like the
# session secret; the variable below is the secret's NAME. The repository is
# public, so Actions logs are public and `run` prints whole commands: a value
# put in the name variable would be printed inside --set-secrets, which is why
# a name that looks like a client secret is refused, and so is a deploy
# environment that carries the secret itself.
GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:-}"
GOOGLE_OAUTH_REDIRECT_URI="${GOOGLE_OAUTH_REDIRECT_URI:-}"
GOOGLE_OAUTH_CLIENT_SECRET_SECRET="${GOOGLE_OAUTH_CLIENT_SECRET_SECRET:-google-oauth-client-secret}"
GOOGLE_SECRET_REF=""
GOOGLE_ENV=""
GOOGLE_NOTE="off"
if [ -n "${GOOGLE_OAUTH_CLIENT_SECRET:-}" ]; then
  echo "GOOGLE_OAUTH_CLIENT_SECRET must not be set in the deploy environment: the client secret is read from Secret Manager (GOOGLE_OAUTH_CLIENT_SECRET_SECRET names it)" >&2
  exit 2
fi
if [ -n "$GOOGLE_OAUTH_CLIENT_ID" ] || [ -n "$GOOGLE_OAUTH_REDIRECT_URI" ]; then
  # None of the three values is echoed in an error: they end up in a public log.
  if [[ ! "$GOOGLE_OAUTH_CLIENT_ID" =~ ^[A-Za-z0-9.-]+\.apps\.googleusercontent\.com$ ]]; then
    echo "GOOGLE_OAUTH_CLIENT_ID is missing or is not a Google client id (…apps.googleusercontent.com)" >&2
    exit 2
  fi
  if [[ ! "$GOOGLE_OAUTH_REDIRECT_URI" =~ ^https://[A-Za-z0-9.-]+/auth/google/callback$ ]]; then
    echo "GOOGLE_OAUTH_REDIRECT_URI is missing or is not https://<host>/auth/google/callback" >&2
    exit 2
  fi
  if [[ ! "$GOOGLE_OAUTH_CLIENT_SECRET_SECRET" =~ ^[A-Za-z][A-Za-z0-9_-]{0,254}$ ]] \
     || [[ "$GOOGLE_OAUTH_CLIENT_SECRET_SECRET" == GOCSPX-* ]]; then
    echo "GOOGLE_OAUTH_CLIENT_SECRET_SECRET must be a Secret Manager secret NAME, never a client secret" >&2
    exit 2
  fi
  GOOGLE_SECRET_REF=",GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_OAUTH_CLIENT_SECRET_SECRET}:latest"
  GOOGLE_ENV=",GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID},GOOGLE_OAUTH_REDIRECT_URI=${GOOGLE_OAUTH_REDIRECT_URI}"
  GOOGLE_NOTE="on (client secret from Secret Manager secret ${GOOGLE_OAUTH_CLIENT_SECRET_SECRET})"
fi

# ── GM tools (po56) ────────────────────────────────────────────────────────────
# OFF unless WORKBENCH_ENABLED_TOOLS is set (a CI repository VARIABLE, like the
# Google id above). Unset, empty or blank means NO tool runs: every tool answers
# 409 tool_disabled. Carried HERE for the same reason as the Google values — the
# --set-env-vars flag REPLACES the service's whole env on every deploy, so a value
# set with `gcloud run services update` alone is wiped by the next CI push.
#
# A comma list of registry tool ids ("npc,loot"). Each id is checked against
# KNOWN_TOOL_IDS BEFORE docker or gcloud runs, and an unknown one fails the
# deploy: the service would otherwise refuse to start on it, leaving the previous
# revision serving while the job looked green. KNOWN_TOOL_IDS cannot import the
# registry, so a test (tests/test_deploy_contract.py) pins it to `ToolId`.
# An id with no server executor yet (docs/deploy-gcp.md section 15) is valid and
# inert: it answers 409 tool_disabled.
#
# Capabilities are deliberately NOT an input of this script: WORKBENCH_CAPABILITIES
# (image_generation) is never forwarded, so the paid portrait and map tools stay
# off until the entitlement gate that covers them ships (D-3, yje.4.1).
#
# A value is never echoed: the repository is public, so Actions logs are public.
KNOWN_TOOL_IDS="npc monster loot names rules portrait encounter hooks recap map"
ENABLED_TOOLS="${WORKBENCH_ENABLED_TOOLS:-}"
TOOLS_LIST=""
TOOLS_ENV=""
TOOLS_NOTE="off"
ENV_DELIM=""
ENV_SEP=","
IFS=',' read -r -a _requested_tools <<< "${ENABLED_TOOLS//$'
'/ }"
for _tool in ${_requested_tools[@]+"${_requested_tools[@]}"}; do
  _tool="${_tool#"${_tool%%[![:space:]]*}"}"   # trim leading whitespace
  _tool="${_tool%"${_tool##*[![:space:]]}"}"   # trim trailing whitespace
  [ -n "$_tool" ] || continue
  case " ${KNOWN_TOOL_IDS} " in
    *" ${_tool} "*) ;;
    *)
      echo "WORKBENCH_ENABLED_TOOLS names a tool the registry does not have; use a comma list of: ${KNOWN_TOOL_IDS// /, }" >&2
      exit 2
      ;;
  esac
  case ",${TOOLS_LIST}," in
    *",${_tool},"*) ;;
    *) TOOLS_LIST="${TOOLS_LIST:+${TOOLS_LIST},}${_tool}" ;;
  esac
done
if [ -n "$TOOLS_LIST" ]; then
  # gcloud splits --set-env-vars on commas, so a list of ids would be read as
  # further KEY=VALUE pairs. `^;^` makes ';' the delimiter for the whole flag;
  # the Google values (validated above to hold neither ',' nor ';') follow suit.
  ENV_DELIM="^;^"
  ENV_SEP=";"
  GOOGLE_ENV="${GOOGLE_ENV//,/;}"
  TOOLS_ENV=";WORKBENCH_ENABLED_TOOLS=${TOOLS_LIST}"
  TOOLS_NOTE="on (${TOOLS_LIST})"
fi

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
echo "  google=${GOOGLE_NOTE}"
echo "  tools=${TOOLS_NOTE}"
if [ "$DRY_RUN" = "1" ]; then
  echo "  (dry-run: printing commands, executing nothing)"
fi

# 1. Build the single-container image (Dockerfile.cloud). Cloud Run is linux/amd64.
run docker build --platform linux/amd64 -f Dockerfile.cloud -t "${IMAGE}" .

# 2. Push to Artifact Registry (operator/CI has run `gcloud auth configure-docker`).
run docker push "${IMAGE}"

# 2b. Resolve the digest we just pushed. Cloud Run records the RESOLVED digest
#     on the revision, not the tag, so the tag alone cannot verify what is
#     serving (x5bz.1.8). RepoDigests is populated by the push above.
if [ "$DRY_RUN" != "1" ]; then
  IMAGE_DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' "${IMAGE}" 2>/dev/null || echo "")"
  echo "  digest=${IMAGE_DIGEST:-<unresolved>}"
fi

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
  --set-secrets "OPENAI_API_KEY=${OPENAI_SECRET}:latest,DATABASE_URL=${DATABASE_URL_SECRET}:latest,SESSION_SECRET=${SESSION_SECRET_SECRET}:latest${GOOGLE_SECRET_REF}" \
  --set-env-vars "${ENV_DELIM}AUTH_TRUSTED_PROXY_HOPS=1${ENV_SEP}GCP_PROJECT=${PROJECT}${GOOGLE_ENV}${TOOLS_ENV}" \
  --timeout 300 \
  --max-instances 2 \
  --memory 1Gi \
  --concurrency 20

# Publish what was actually deployed, so a following step can VERIFY it rather
# than reconstruct it (x5bz.1.7). Reconstruction would mean duplicating REGION,
# PROJECT, AR_REPO and the sha rule above — four defaults that live only here, so
# changing any one of them would silently make the verifier check a string that
# was never pushed. Only on a real deploy: a dry run pushed nothing.
if [ "$DRY_RUN" != "1" ] && [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "image=${IMAGE}"
    echo "image_digest=${IMAGE_DIGEST:-}"
    echo "region=${REGION}"
    echo "project=${PROJECT}"
    echo "service=${SERVICE}"
  } >> "$GITHUB_OUTPUT"
fi

echo "Done."
