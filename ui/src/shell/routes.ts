/**
 * routes -- the client-side route table (agent-forge-harness-y40).
 *
 * The path names the SCREEN; a single-use credential still rides in the
 * fragment (see `inviteToken.ts`'s `readFragmentToken` / `RESERVED_FRAGMENT_KEYS`)
 * -- this table carries no such key on purpose. A row is `{ path, screen,
 * coldLoad }`, three fields, no more: paths are exact matches, with no
 * parameters and no wildcards.
 *
 * `service.spa_fallback.CLIENT_ROUTES` is the server half of this table, and
 * `tests/test_spa_routes_parity.py` fails if the two ever drift.
 *
 * Adding a route: add one row here AND one entry in
 * `service/spa_fallback.py`'s `CLIENT_ROUTES` (same order), then see
 * `docs/adr/client-routing.md` for the fragment-key step if the new screen
 * reads a single-use credential.
 */
import type { Screen } from './AppNav'

export interface RouteRow {
  readonly path: string
  readonly screen: Screen
  /**
   * What a COLD load of this path does:
   *  - 'restore': render this screen directly.
   *  - 'home': render Landing instead, and replace the URL with '/'. Used
   *    for '/workspace' today because reloading has always returned to
   *    Landing (`ui/e2e/session.spec.ts`), and restoring a workspace from a
   *    URL is `1kg.2.5`'s decision, not this table's.
   */
  readonly coldLoad: 'restore' | 'home'
}

export const ROUTES: readonly RouteRow[] = [
  { path: '/', screen: 'landing', coldLoad: 'restore' },
  { path: '/workspace', screen: 'workspace', coldLoad: 'home' },
  { path: '/profile', screen: 'profile', coldLoad: 'restore' },
]

/** The row for an exact pathname, or `null` if there is none. */
export function routeForPath(pathname: string): RouteRow | null {
  return ROUTES.find((row) => row.path === pathname) ?? null
}

/** The path for a screen. Every `Screen` has exactly one row today. */
export function pathForScreen(screen: Screen): string {
  const row = ROUTES.find((r) => r.screen === screen)
  return row ? row.path : '/'
}

export interface BootResult {
  readonly screen: Screen
  /** `null` if the address bar already matches; otherwise the path to
   * `replaceState` it to. */
  readonly replacePath: string | null
}

/**
 * What a COLD load at `pathname` should render, computed once and
 * synchronously so the first paint is already correct (see `AppRoot.tsx`).
 *
 *  - a known path with `coldLoad: 'restore'` -> that screen, URL unchanged.
 *  - a known path with `coldLoad: 'home'` -> Landing, URL replaced with '/'.
 *  - an unknown path -> Landing, URL replaced with '/' (matches what every
 *    path renders today via nginx's/Vite's own SPA fallback; production
 *    never reaches the SPA for an unknown path at all).
 */
export function startScreen(pathname: string): BootResult {
  const row = routeForPath(pathname)
  if (row === null) return { screen: 'landing', replacePath: '/' }
  if (row.coldLoad === 'home') return { screen: 'landing', replacePath: '/' }
  return { screen: row.screen, replacePath: null }
}
