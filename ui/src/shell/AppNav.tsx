/**
 * AppNav — Application-level navigation context.
 *
 * Manages which screen is active (landing vs workspace) and which chat mode
 * is selected. Provides the AppNavProvider, AppNavContext, and useAppNav hook.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import * as React from 'react'

// ── Types ─────────────────────────────────────────────────────────────────────

export type Screen = 'landing' | 'workspace' | 'profile'
export type ChatMode = 'sage' | 'spell' | 'rules' | 'gm'

/**
 * How a screen change should reach the URL (agent-forge-harness-y40, R8):
 * `push` for a user navigation (adds a history entry, so Back steps through
 * it); `replace` for the identity-reset in App.tsx (it is not a navigation,
 * and must not leave a history entry a signed-out user could Back into).
 */
export type NavIntent = 'push' | 'replace'

export interface AppNavState {
  screen: Screen
  mode: ChatMode
  conversationId: string | null
  enterWorkspace: (mode?: ChatMode) => void
  setMode: (mode: ChatMode) => void
  setConversationId: (id: string | null) => void
  backToLanding: (intent?: NavIntent) => void
  /** Open the profile screen (swe1.7). */
  openProfile: () => void
  /** Return to the workspace without disturbing the active mode (swe1.7). */
  backToWorkspace: () => void
  /**
   * How the MOST RECENT screen change above should reach the URL
   * (agent-forge-harness-y40). Optional so `.storybook/shellHarness.tsx`'s
   * hand-built `AppNavState` still type-checks unedited; `UrlNavigation.tsx`
   * is the only reader.
   */
  navIntent?: NavIntent
}

// ── Context ───────────────────────────────────────────────────────────────────

const defaultState: AppNavState = {
  screen: 'landing',
  mode: 'sage',
  conversationId: null,
  enterWorkspace: () => {},
  setMode: () => {},
  setConversationId: () => {},
  backToLanding: () => {},
  openProfile: () => {},
  backToWorkspace: () => {},
}

// eslint-disable-next-line react-refresh/only-export-components -- context co-located with provider; HMR-only rule
export const AppNavContext = createContext<AppNavState>(defaultState)

// ── Provider ──────────────────────────────────────────────────────────────────

/**
 * PARALLEL IMPLEMENTATION — `.storybook/shellHarness.tsx` re-implements this
 * state machine rather than wrapping `AppNavProvider`, so that a story can
 * start at any `screen`/`mode`/`conversationId` without driving the UI there
 * first. The two agree today; they are not kept in step by anything, so a
 * transition added below has to be added there too or the stories will be
 * exercising a shell that no longer matches the product
 * (agent-forge-harness-27h review, note F8).
 */
interface AppNavProviderProps {
  children: ReactNode
  /**
   * Cold-load screen (agent-forge-harness-y40): set once, synchronously,
   * from the URL so the first paint already shows the right screen -- see
   * `ui/src/shell/routes.ts`'s `startScreen` and `ui/src/AppRoot.tsx`.
   * Defaults to `'landing'` so every existing caller (tests, Storybook) is
   * unaffected.
   */
  initialScreen?: Screen
}

export function AppNavProvider(
  { children, initialScreen = 'landing' }: AppNavProviderProps,
): React.JSX.Element {
  const [screen, setScreen] = useState<Screen>(initialScreen)
  const [mode, setModeState] = useState<ChatMode>('sage')
  const [conversationId, setConversationIdState] = useState<string | null>(null)
  // Starts 'replace' (agent-forge-harness-y40): the initial screen was
  // PLACED there by the boot computation, not pushed by a user action, so
  // UrlNavigation's very first correction (e.g. a cold load of /workspace
  // resolving to landing/'/') must not leave a history entry behind either.
  const [navIntent, setNavIntent] = useState<NavIntent>('replace')

  const enterWorkspace = useCallback((nextMode: ChatMode = 'sage') => {
    setModeState(nextMode)
    setScreen('workspace')
    setNavIntent('push')
  }, [])

  const setMode = useCallback((nextMode: ChatMode) => {
    setModeState(nextMode)
  }, [])

  const setConversationId = useCallback((id: string | null) => {
    setConversationIdState(id)
  }, [])

  const backToLanding = useCallback((intent: NavIntent = 'push') => {
    setScreen('landing')
    setNavIntent(intent)
  }, [])

  const openProfile = useCallback(() => {
    setScreen('profile')
    setNavIntent('push')
  }, [])

  // Return to the workspace without touching `mode` (unlike enterWorkspace,
  // which resets it to sage) — a DM opening Profile from the GM channel returns to GM.
  const backToWorkspace = useCallback(() => {
    setScreen('workspace')
    setNavIntent('push')
  }, [])

  const value = useMemo<AppNavState>(
    () => ({
      screen,
      mode,
      conversationId,
      enterWorkspace,
      setMode,
      setConversationId,
      backToLanding,
      openProfile,
      backToWorkspace,
      navIntent,
    }),
    [
      screen,
      mode,
      conversationId,
      enterWorkspace,
      setMode,
      setConversationId,
      backToLanding,
      openProfile,
      backToWorkspace,
      navIntent,
    ],
  )

  return <AppNavContext.Provider value={value}>{children}</AppNavContext.Provider>
}

// ── Hook ──────────────────────────────────────────────────────────────────────

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useAppNav(): AppNavState {
  return useContext(AppNavContext)
}
