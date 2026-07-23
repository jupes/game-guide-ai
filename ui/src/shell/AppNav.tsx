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

export interface AppNavState {
  screen: Screen
  mode: ChatMode
  conversationId: string | null
  enterWorkspace: (mode?: ChatMode) => void
  setMode: (mode: ChatMode) => void
  setConversationId: (id: string | null) => void
  backToLanding: () => void
  /** Open the profile screen (swe1.7). */
  openProfile: () => void
  /** Return to the workspace without disturbing the active mode (swe1.7). */
  backToWorkspace: () => void
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

interface AppNavProviderProps {
  children: ReactNode
}

export function AppNavProvider({ children }: AppNavProviderProps): React.JSX.Element {
  const [screen, setScreen] = useState<Screen>('landing')
  const [mode, setModeState] = useState<ChatMode>('sage')
  const [conversationId, setConversationIdState] = useState<string | null>(null)

  const enterWorkspace = useCallback((nextMode: ChatMode = 'sage') => {
    setModeState(nextMode)
    setScreen('workspace')
  }, [])

  const setMode = useCallback((nextMode: ChatMode) => {
    setModeState(nextMode)
  }, [])

  const setConversationId = useCallback((id: string | null) => {
    setConversationIdState(id)
  }, [])

  const backToLanding = useCallback(() => {
    setScreen('landing')
  }, [])

  const openProfile = useCallback(() => {
    setScreen('profile')
  }, [])

  // Return to the workspace without touching `mode` (unlike enterWorkspace,
  // which resets it to sage) — a DM opening Profile from the GM channel returns to GM.
  const backToWorkspace = useCallback(() => {
    setScreen('workspace')
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
    ],
  )

  return <AppNavContext.Provider value={value}>{children}</AppNavContext.Provider>
}

// ── Hook ──────────────────────────────────────────────────────────────────────

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useAppNav(): AppNavState {
  return useContext(AppNavContext)
}
