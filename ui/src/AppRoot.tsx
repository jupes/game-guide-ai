import * as React from 'react'
import App from './App'
import { ThemeProvider } from './ds/theme'
import { AppNavProvider } from './shell/AppNav'
import { CampaignProvider } from './shell/campaignContext'
import { CurrentUserProvider } from './shell/currentUser'
import { ConversationStoreProvider } from './shell/ConversationStoreContext'
import { UrlNavigation } from './shell/UrlNavigation'
import { startScreen } from './shell/routes'
import { readCampaignRestore } from './shell/workspaceFragment'

/**
 * AppRoot -- the shipped provider tower (agent-forge-harness-y40, R13a).
 *
 * `main.tsx` renders this and nothing else, and `appRouting.test.tsx` mounts
 * this SAME component -- not a tower the test built for itself -- so "the
 * cold load works" is proven about the app that actually ships.
 */
export function AppRoot(): React.JSX.Element {
  // Computed once, synchronously, so the cold-loaded screen is right at
  // FIRST render (no flash of Landing before correcting to /profile, etc).
  // A `/workspace` URL whose fragment names a campaign opens the GM channel
  // and hands the restore to CampaignProvider, which asks the server before
  // anything campaign-scoped happens (agent-forge-harness-1kg.2.5, brief
  // section 7.4); every other URL boots exactly as before. Held here, above
  // the account-keyed campaign state, so it is read once per page load.
  const [boot] = React.useState(() => ({
    route: startScreen(window.location.pathname),
    restore: readCampaignRestore(window.location.pathname, window.location.hash),
  }))
  return (
    <ThemeProvider>
      <AppNavProvider
        initialScreen={boot.restore === null ? boot.route.screen : 'workspace'}
        initialMode={boot.restore === null ? 'sage' : 'gm'}
      >
        <CurrentUserProvider>
          <ConversationStoreProvider>
            <CampaignProvider restore={boot.restore}>
              <UrlNavigation />
              <App />
            </CampaignProvider>
          </ConversationStoreProvider>
        </CurrentUserProvider>
      </AppNavProvider>
    </ThemeProvider>
  )
}
