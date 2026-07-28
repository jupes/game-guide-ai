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
#     AUTH_RATE_LIMIT_PER_SOURCE  ×  (sum of maxScale over traffic-serving revisions)
#
# Sending fewer than that reports a false FAILURE: with the shipped settings
# (30, --max-instances 2) a 40-attempt probe split 20/20 across two instances
# returns forty 401s from a perfectly configured deployment. The count is
# derived below from the live service rather than hardcoded, per revision — and
# the script refuses to run at all when it cannot exhaust the budget it derived.
#
# Cost: this spends the source budget for whatever address the service really
# sees, so YOUR OWN sign-ins get 429 for AUTH_RATE_LIMIT_WINDOW_S (default 5
# min). It decays on its own; there is no lockout to clear.
#
# Usage:  bash scripts/verify-auth-throttle.sh https://<service-url>
#         ATTEMPTS=200 bash scripts/verify-auth-throttle.sh https://<url>
#         PER_SOURCE=30 CAPACITY=4 bash scripts/...   # state the topology, skip gcloud

set -euo pipefail

BASE="${1:?usage: verify-auth-throttle.sh <service-url> [attempts]}"
SERVICE="${SERVICE:-game-guide-ai}"
REGION="${GCP_REGION:-us-central1}"
PER_SOURCE="${PER_SOURCE:-30}"        # config.AUTH_RATE_LIMIT_PER_SOURCE
CAPACITY="${CAPACITY:-}"              # total instance slots across serving revisions

# Total instance slots = sum of each traffic-serving revision's OWN maxScale.
#
# Two traps here, both of which under-count and so produce a false FAILURE:
#   - `--format='value(status.traffic[].percent)'` joins repeated fields with
#     SEMICOLONS, not tabs or newlines, so a 50/50 split arrives as the single
#     token "50;50" and counts as one revision. `--flatten` gives one record per
#     element instead, which is the only form safe to count.
#   - revisions do not have to share the current template's maxScale — an older
#     revision still taking traffic keeps the limit it was deployed with — so
#     each one is read individually rather than assumed.
derive_capacity() {
  local total=0 rev pct max
  while read -r rev pct; do
    [ -n "${rev}" ] || continue
    [ "${pct:-0}" -gt 0 ] 2>/dev/null || continue
    max=$(gcloud run revisions describe "${rev}" --region "${REGION}" \
      --format='value(metadata.annotations["autoscaling.knative.dev/maxScale"])' 2>/dev/null || true)
    # No maxScale means unbounded autoscaling: there is no budget to derive.
    [ -n "${max}" ] || return 1
    total=$(( total + max ))
  done < <(gcloud run services describe "${SERVICE}" --region "${REGION}" \
             --flatten='status.traffic[]' \
             --format='value[separator=" "](status.traffic.revisionName, status.traffic.percent)' \
             2>/dev/null)
  [ "${total}" -gt 0 ] || return 1
  echo "${total}"
}

if [ -z "${CAPACITY}" ]; then
  CAPACITY=$(derive_capacity || true)
  if [ -z "${CAPACITY}" ]; then
    CAPACITY=2
    echo "note: could not read the live traffic split / maxScale (unbounded autoscaling,"
    echo "      or gcloud unavailable). Assuming ${CAPACITY} instance slots — set CAPACITY"
    echo "      explicitly before trusting a FAIL result."
  fi
fi

# Total budget, plus a margin so an uneven split across instances still trips it.
BUDGET=$(( PER_SOURCE * CAPACITY ))
ATTEMPTS="${ATTEMPTS:-${2:-$(( BUDGET + PER_SOURCE ))}}"

echo "Rate-limit budget to exhaust: ${PER_SOURCE} per source × ${CAPACITY} instance slot(s) = ${BUDGET}"
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
echo "       CAPACITY set explicitly before trusting this result."
exit 1
