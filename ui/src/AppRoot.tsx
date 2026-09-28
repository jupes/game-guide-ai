import * as React from 'react'
import App from './App'
import { ThemeProvider } from './ds/theme'
import { AppNavProvider } from './shell/AppNav'
import { CurrentUserProvider } from './shell/currentUser'
import { ConversationStoreProvider } from './shell/ConversationStoreContext'
import { UrlNavigation } from './shell/UrlNavigation'
import { startScreen } from './shell/routes'

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
  const [boot] = React.useState(() => startScreen(window.location.pathname))
  return (
    <ThemeProvider>
      <AppNavProvider initialScreen={boot.screen}>
        <CurrentUserProvider>
          <ConversationStoreProvider>
            <UrlNavigation />
            <App />
          </ConversationStoreProvider>
        </CurrentUserProvider>
      </AppNavProvider>
    </ThemeProvider>
  )
}
