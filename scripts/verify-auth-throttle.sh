#!/usr/bin/env bash
# Verify, against a DEPLOYED service, that auth throttling keys on something the
# caller cannot choose.
#
# Why this exists: reading a request log only proves what Google observed, not
# what `client_source()` selected from the X-Forwarded-For chain — hop counts of
# 0, 1 or a badly wrong value all produce identical-looking log lines. The only
# check that distinguishes them is behavioral: send attempts that differ ONLY in
# a spoofed X-Forwarded-For and see whether they share a budget.
#
#   pass  -> a 429 appears. The spoofed values were ignored; the key is real.
#   FAIL  -> every attempt returns 401. Each spoofed value bought its own budget,
#            so AUTH_TRUSTED_PROXY_HOPS reaches past the trusted hops into
#            caller-written text (or ingress lets the caller be the proxy).
#
# Emails are rotated too, so the ACCOUNT limiter cannot be what trips — this
# isolates the source key, exactly the attack the header spoof enables. No real
# account is touched or locked out.
#
# ── HOW MANY ATTEMPTS ─────────────────────────────────────────────────────────
# The limiter is per PROCESS, and requests are spread over every serving
# container, so the budget an external caller has to exhaust is
#
#     AUTH_RATE_LIMIT_PER_SOURCE  ×  instances  ×  revisions serving traffic
#
# Sending fewer than that reports a false FAILURE: with the shipped settings
# (30, --max-instances 2) a 40-attempt probe split 20/20 across two instances
# returns forty 401s from a perfectly configured deployment. The count is
# derived below rather than hardcoded, and the instance/revision numbers are
# read from the live service when gcloud is available.
#
# Cost: this spends the source budget for whatever address the service really
# sees, so YOUR OWN sign-ins get 429 for AUTH_RATE_LIMIT_WINDOW_S (default 5
# min). It decays on its own; there is no lockout to clear.
#
# Usage:  bash scripts/verify-auth-throttle.sh https://<service-url>
#         ATTEMPTS=200 bash scripts/verify-auth-throttle.sh https://<url>
#         PER_SOURCE=30 INSTANCES=2 REVISIONS=1 bash scripts/... # skip gcloud

set -euo pipefail

BASE="${1:?usage: verify-auth-throttle.sh <service-url> [attempts]}"
SERVICE="${SERVICE:-game-guide-ai}"
REGION="${GCP_REGION:-us-central1}"
PER_SOURCE="${PER_SOURCE:-30}"        # config.AUTH_RATE_LIMIT_PER_SOURCE
INSTANCES="${INSTANCES:-}"            # deploy.sh --max-instances
REVISIONS="${REVISIONS:-}"            # revisions with a non-zero traffic split

# Read the live topology when we can; fall back to the shipped defaults loudly.
if [ -z "${INSTANCES}" ]; then
  INSTANCES=$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
    --format='value(spec.template.metadata.annotations["autoscaling.knative.dev/maxScale"])' \
    2>/dev/null || true)
  [ -n "${INSTANCES}" ] || { INSTANCES=2; echo "note: could not read --max-instances, assuming ${INSTANCES}"; }
fi
if [ -z "${REVISIONS}" ]; then
  REVISIONS=$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
    --format='value(status.traffic[].percent)' 2>/dev/null | tr '\t' '\n' | grep -cve '^0\?$' || true)
  [ "${REVISIONS:-0}" -ge 1 ] 2>/dev/null || { REVISIONS=1; echo "note: could not read traffic split, assuming ${REVISIONS} revision"; }
fi

# Total budget, plus a margin so an uneven split across instances still trips it.
BUDGET=$(( PER_SOURCE * INSTANCES * REVISIONS ))
ATTEMPTS="${ATTEMPTS:-${2:-$(( BUDGET + PER_SOURCE ))}}"

echo "Rate-limit budget to exhaust: ${PER_SOURCE} × ${INSTANCES} instance(s) × ${REVISIONS} revision(s) = ${BUDGET}"
echo "Probing ${BASE}/auth/login with up to ${ATTEMPTS} attempts, rotating BOTH the"
echo "spoofed X-Forwarded-For and the email..."

if [ "${ATTEMPTS}" -le "${BUDGET}" ]; then
  echo "REFUSING: ${ATTEMPTS} attempts cannot exhaust a budget of ${BUDGET} — this"
  echo "          would report a false failure. Raise ATTEMPTS above ${BUDGET}."
  exit 2
fi

saw_429=0
for i in $(seq 1 "${ATTEMPTS}"); do
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/auth/login" \
    -H 'Content-Type: application/json' \
    -H "X-Forwarded-For: 198.51.100.$(( (i % 250) + 1 ))" \
    -d "{\"email\":\"throttle-probe-${i}@example.invalid\",\"password\":\"not-a-real-password\"}")
  printf '%s ' "${code}"
  if [ "${code}" = "429" ]; then saw_429=1; break; fi
done
echo

if [ "${saw_429}" = "1" ]; then
  echo "PASS — spoofed X-Forwarded-For values shared one budget, so the source"
  echo "       key is not caller-controlled."
  echo
  echo "Now confirm the key is the RIGHT one (not everyone collapsed into a"
  echo "single bucket, which also passes the test above). The throttle entry"
  echo "carries the derived key and a trace; join it to the request log entry,"
  echo "which is where the address Google observed lives:"
  echo
  echo "  gcloud logging read 'jsonPayload.message=\"auth attempt throttled\"' \\"
  echo "    --limit 1 --format='value(jsonPayload.source, trace)'"
  echo "  gcloud logging read 'logName:\"run.googleapis.com%2Frequests\" AND trace=\"<TRACE>\"' \\"
  echo "    --limit 1 --format='value(httpRequest.remoteIp)'"
  echo
  echo "  The two addresses must match. If every tester's entries show the SAME"
  echo "  jsonPayload.source, hops is too low. See docs/deploy-gcp.md §9."
  exit 0
fi

echo "FAIL — ${ATTEMPTS} attempts from rotating spoofed addresses, no 429, against"
echo "       a budget of ${BUDGET}. Each spoofed value bought a fresh budget."
echo "       Check that AUTH_TRUSTED_PROXY_HOPS matches the real proxy chain (1"
echo "       for the run.app front end, 2 behind an external HTTPS load balancer)"
echo "       and that ingress cannot be bypassed. See docs/deploy-gcp.md §9."
echo "       If the topology numbers above look wrong, re-run with PER_SOURCE /"
echo "       INSTANCES / REVISIONS set explicitly before trusting this result."
exit 1
