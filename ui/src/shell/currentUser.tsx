/**
 * currentUser — Stub current user context.
 *
 * Provides a guest "Adventurer" stub for the shell during development.
 * In a real app, replace STUB with a real auth integration.
 */

import { createContext, useContext, useMemo, type ReactNode } from 'react'
import * as React from 'react'

// ── Types ─────────────────────────────────────────────────────────────────────

export interface CurrentUser {
  id: string
  displayName: string
  initials: string
  signOut(): void
  editProfile(): void
}

export interface CurrentUserContextValue {
  user: CurrentUser
}

// ── Stub ──────────────────────────────────────────────────────────────────────

function noop(): void {}

// eslint-disable-next-line react-refresh/only-export-components -- stub constant co-located with provider; HMR-only rule
export const STUB: CurrentUser = {
  id: 'guest',
  displayName: 'Adventurer',
  initials: 'AV',
  signOut: noop,
  editProfile: noop,
}

// ── Context ───────────────────────────────────────────────────────────────────

// Default is null so a hook call outside a provider is a hard error (matches
// useTheme), rather than silently resolving to the STUB and masking a nesting bug.
// eslint-disable-next-line react-refresh/only-export-components -- context co-located with provider; HMR-only rule
export const CurrentUserContext = createContext<CurrentUserContextValue | null>(null)

// ── Provider ──────────────────────────────────────────────────────────────────

interface CurrentUserProviderProps {
  children: ReactNode
}

export function CurrentUserProvider({ children }: CurrentUserProviderProps): React.JSX.Element {
  const value = useMemo<CurrentUserContextValue>(() => ({ user: STUB }), [])
  return <CurrentUserContext.Provider value={value}>{children}</CurrentUserContext.Provider>
}

// ── Hook ──────────────────────────────────────────────────────────────────────

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useCurrentUser(): CurrentUserContextValue {
  const ctx = useContext(CurrentUserContext)
  if (ctx === null) {
    throw new Error('useCurrentUser must be used within a <CurrentUserProvider>.')
  }
  return ctx
}
