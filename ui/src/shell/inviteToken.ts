/**
 * inviteToken -- read a single-use credential from the URL fragment (x5bz.2;
 * generalized by agent-forge-harness-y40).
 *
 * A single-use credential (an invite, and -- once their beads read this --
 * an email-verification or password-reset token) travels in the
 * **fragment**, never the query string. A fragment is never sent to the
 * server, so it stays out of Cloud Run's automatic request logs (Cloud
 * Logging's `httpRequest.requestUrl` includes the query portion) -- with
 * `?invite=`, an outstanding invite was exposed to anyone with log access
 * for the whole signup window. Scrubbing it client-side can't help: by then
 * the request has already been logged.
 *
 * `invite` and `token` are reserved fragment keys for exactly this purpose
 * (`RESERVED_FRAGMENT_KEYS`, agent-forge-harness-y40 R7/I-4); every other key
 * in the one fragment grammar belongs to `1kg.6.3` (CANVAS-30).
 * `ui/src/shell/UrlNavigation.tsx` scrubs both reserved keys from the address
 * bar on load, on WHATEVER path they arrive -- the built UI's client router
 * (agent-forge-harness-y40) now resolves `/`, `/workspace` and `/profile` on
 * a cold load; a path outside that allowlist still 404s in production (see
 * `docs/adr/client-routing.md`).
 */

/** Read `key` out of a URL fragment. Returns `null` if the key is absent, its
 * value is empty, or `hash` looks like a query string (see below). */
export function readFragmentToken(hash: string, key: string): string | null {
  // Defensive: refuse a query string outright. URLSearchParams happily strips
  // a leading '?', so passing `location.search` by mistake would silently
  // "work" and reintroduce the logged-credential problem this exists to
  // avoid.
  if (hash.startsWith('?')) return null
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const token = new URLSearchParams(raw).get(key)
  return token && token.length > 0 ? token : null
}

/** The invite flow's own reader. Kept as a thin, byte-identical wrapper --
 * same name, same signature, same behaviour -- so `App.tsx`'s existing
 * invite handling and `inviteToken.test.ts` need no changes. */
export function getInviteTokenFromHash(hash: string): string | null {
  return readFragmentToken(hash, 'invite')
}

/** Fragment keys reserved for single-use credentials (agent-forge-harness-y40
 * R7/I-4). Do not add a workspace key here -- `1kg.6.3` owns every other key
 * in the one fragment grammar (CANVAS-30). */
export const RESERVED_FRAGMENT_KEYS: readonly string[] = ['invite', 'token']

/** Strip every reserved key out of a fragment, leaving every other key
 * untouched. Returns the remaining fragment WITHOUT a leading '#' (`''` if
 * nothing is left). Used by `UrlNavigation` so a single-use credential never
 * lingers in browser history, regardless of which path it arrived on. */
export function scrubReservedFragmentKeys(hash: string): string {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  if (raw === '') return ''
  const params = new URLSearchParams(raw)
  for (const key of RESERVED_FRAGMENT_KEYS) {
    params.delete(key)
  }
  return params.toString()
}
