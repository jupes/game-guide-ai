/**
 * inviteToken — read the one-time invite token from the URL (x5bz.2).
 *
 * The token is delivered in the **fragment** (`/#invite=<token>`), not the query
 * string. A fragment is never sent to the server, so the credential stays out of
 * Cloud Run's automatic request logs (Cloud Logging's `httpRequest.requestUrl`
 * includes the query portion) — with `?invite=`, an outstanding invite was
 * exposed to anyone with log access for the whole signup window. Scrubbing it
 * client-side can't help: by then the request has already been logged.
 *
 * The root path is still used because the SPA has no client router and the built
 * UI 404s on deeper paths. This is the pure parser both App and its tests use.
 */

export function getInviteTokenFromHash(hash: string): string | null {
  // Defensive: refuse a query string outright. URLSearchParams happily strips a
  // leading '?', so passing `location.search` by mistake would silently "work"
  // and reintroduce the logged-credential problem this exists to avoid.
  if (hash.startsWith('?')) return null
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const token = new URLSearchParams(raw).get('invite')
  return token && token.length > 0 ? token : null
}
