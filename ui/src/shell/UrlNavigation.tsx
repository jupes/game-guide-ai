import * as React from 'react'
import { useAppNav } from './AppNav'
import { pathForScreen, routeForPath } from './routes'
import { scrubReservedFragmentKeys } from './inviteToken'

/**
 * UrlNavigation -- binds AppNav's screen state to the browser URL
 * (agent-forge-harness-y40).
 *
 * Deliberately NOT inside `App` or `AppNavProvider`: Storybook renders `App`
 * at `/iframe.html` (`App.stories.tsx`, `AppNav.stories.tsx`), and a boot
 * rewrite living there would hijack Storybook's own URL. `AppRoot` mounts
 * this component alongside `App` instead, so only the real, shipped app is
 * ever routed.
 *
 * Renders nothing.
 */
export function UrlNavigation(): null {
  const { screen, navIntent, backToLanding, backToWorkspace, openProfile } = useAppNav()

  // Boot-only: scrub a single-use credential (R7) out of the fragment no
  // matter which path we cold-loaded at. The PATH needs no equivalent
  // one-off effect here: AppNavProvider's initial `navIntent` is 'replace',
  // so the ordinary screen-sync effect below already performs R3/R6's
  // cold-load correction (e.g. `/workspace` -> `/`) the very first time it
  // runs, because `pathForScreen(initialScreen)` already differs from
  // `location.pathname` exactly when a correction is needed.
  React.useEffect(() => {
    const currentHash = window.location.hash.startsWith('#')
      ? window.location.hash.slice(1)
      : window.location.hash
    const scrubbed = scrubReservedFragmentKeys(window.location.hash)
    if (scrubbed !== currentHash) {
      const nextHash = scrubbed ? `#${scrubbed}` : ''
      window.history.replaceState(
        {}, '',
        window.location.pathname + window.location.search + nextHash,
      )
    }
    // Boot-only, by design: re-running this on every render would re-read a
    // hash we may have already scrubbed away, which is harmless but pointless.
  }, [])

  // Keep the address bar in step with the screen (R4): `push` for a user
  // navigation (Back can step through it), `replace` for the identity reset
  // (App.tsx) and for the boot correction above.
  React.useEffect(() => {
    const target = pathForScreen(screen)
    if (target === window.location.pathname) return
    const url = target + window.location.search + window.location.hash
    if (navIntent === 'replace') {
      window.history.replaceState({}, '', url)
    } else {
      window.history.pushState({}, '', url)
    }
  }, [screen, navIntent])

  // Back/Forward (R4): by the time `popstate` fires, the browser has ALREADY
  // moved `location.pathname`, so reflecting it here must set the screen and
  // write NOTHING back — the effect above will see its target already equal
  // to `location.pathname` and skip the write on its own.
  React.useEffect(() => {
    function onPopState(): void {
      const row = routeForPath(window.location.pathname)
      const nextScreen = row?.screen ?? 'landing'
      if (nextScreen === 'workspace') backToWorkspace()
      else if (nextScreen === 'profile') openProfile()
      else backToLanding()
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [backToWorkspace, openProfile, backToLanding])

  return null
}
