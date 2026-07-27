/**
 * inviteToken — read the one-time invite token from the URL (x5bz.2).
 *
 * The SPA has no client router, and the built UI is served by StaticFiles
 * with no path-based fallback beyond "/" (a deep link like /signup 404s), so
 * the admin invite CLI mints a root-path link instead: `/?invite=<token>`.
 * This is the pure parser both App and its tests use — no window access here,
 * so it's trivially testable with plain strings.
 */

export function getInviteTokenFromSearch(search: string): string | null {
  const params = new URLSearchParams(search)
  const token = params.get('invite')
  return token && token.length > 0 ? token : null
}
