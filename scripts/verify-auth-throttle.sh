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
# Cost: this spends the source budget for whatever address the service really
# sees, so YOUR OWN sign-ins get 429 for AUTH_RATE_LIMIT_WINDOW_S (default 5
# min). It decays on its own; there is no lockout to clear.
#
# Usage:  bash scripts/verify-auth-throttle.sh https://<service-url>
#         bash scripts/verify-auth-throttle.sh https://<url> 40   # attempts

set -euo pipefail

BASE="${1:?usage: verify-auth-throttle.sh <service-url> [attempts]}"
ATTEMPTS="${2:-40}"   # must exceed AUTH_RATE_LIMIT_PER_SOURCE (default 30)

echo "Probing ${BASE}/auth/login with ${ATTEMPTS} attempts, rotating BOTH the"
echo "spoofed X-Forwarded-For and the email..."

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
  echo "single bucket, which also passes the test above). The throttle log line"
  echo "carries the derived key; it must equal the peer address Google observed:"
  echo
  echo "  gcloud logging read \\"
  echo "    'resource.type=\"cloud_run_revision\" AND textPayload:\"auth attempt throttled\"' \\"
  echo "    --limit 3 --format='value(textPayload, httpRequest.remoteIp)'"
  echo
  echo "  source=<A> in the message must match remoteIp <A> on the same entry."
  echo "  If every tester's entries show the SAME source, hops is too low."
  exit 0
fi

echo "FAIL — ${ATTEMPTS} attempts from ${ATTEMPTS} spoofed addresses, no 429."
echo "       Each spoofed value bought a fresh budget. Check that"
echo "       AUTH_TRUSTED_PROXY_HOPS matches the real proxy chain (1 for the"
echo "       run.app front end, 2 behind an external HTTPS load balancer) and"
echo "       that ingress cannot be bypassed. See docs/deploy-gcp.md §9."
exit 1
