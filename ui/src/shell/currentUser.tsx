/**
 * currentUser — Session-backed current user (x5bz.2).
 *
 * Identity (email, role) comes from the server session via GET /auth/me,
 * checked once on mount. `authStatus` tracks that check so App can gate
 * rendering: while `checking`, the app renders normally (avoids a login-screen
 * flash for the common already-signed-in case); once it resolves to
 * `unauthenticated`, App swaps to Login/Signup. Role is server-authoritative
 * and no longer user-settable (replaces the pre-auth localStorage role
 * toggle) — the GM channel gate in the UI is now just a courtesy; the server
 * enforces it for real. displayName/avatarTone remain a local, cosmetic-only
 * stub (per-user profile storage is out of scope for the pilot).
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import * as React from 'react'
import { deriveInitials, type AvatarTone } from '../ds/Avatar'
import { getMe, logout as apiLogout, type AuthUser } from '../api'

// ── Types ─────────────────────────────────────────────────────────────────────

export type UserRole = 'dm' | 'player'
export type AuthStatus = 'checking' | 'authenticated' | 'unauthenticated'

export interface CurrentUser {
  id: string
  displayName: string
  initials: string
  /** Chosen avatar tone (swe1.7). Optional so existing user literals still typecheck. */
  avatarTone?: AvatarTone
  role: UserRole
  signOut(): void
  editProfile(): void
}

export interface CurrentUserContextValue {
  user: CurrentUser
  /** Session-check status; App uses this to gate Login/Signup vs the app. */
  authStatus: AuthStatus
  /** Adopt an authenticated identity (called by Login/Signup on success). */
  signIn: (authUser: AuthUser) => void
  setDisplayName: (name: string) => void
  setAvatarTone: (tone: AvatarTone) => void
}

// ── Guest default (pre-session / while checking) ──────────────────────────────

function noop(): void {}

// eslint-disable-next-line react-refresh/only-export-components -- stub constant co-located with provider; HMR-only rule
export const STUB: CurrentUser = {
  id: 'guest',
  displayName: 'Adventurer',
  initials: 'AV',
  avatarTone: 'gold',
  role: 'player',
  signOut: noop,
  editProfile: noop,
}

// ── Profile persistence (local-stub only; unrelated to the server session) ───
// Real per-user profile storage is out of scope for the pilot.

const PROFILE_STORAGE_KEY = 'game-guide-ai:profile'
const AVATAR_TONES: readonly AvatarTone[] = ['gold', 'ember', 'verdigris', 'arcane']

interface StoredProfile {
  displayName?: string
  avatarTone?: AvatarTone
}

function isAvatarTone(value: unknown): value is AvatarTone {
  return typeof value === 'string' && (AVATAR_TONES as readonly string[]).includes(value)
}

function loadProfile(): StoredProfile {
  try {
    const raw = localStorage.getItem(PROFILE_STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const { displayName, avatarTone } = parsed as StoredProfile
    return {
      displayName: typeof displayName === 'string' ? displayName : undefined,
      avatarTone: isAvatarTone(avatarTone) ? avatarTone : undefined,
    }
  } catch {
    return {}
  }
}

function saveProfile(profile: StoredProfile): void {
  try {
    localStorage.setItem(PROFILE_STORAGE_KEY, JSON.stringify(profile))
  } catch (err) {
    console.warn('currentUser: could not persist profile', err)
  }
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
  const [authStatus, setAuthStatus] = useState<AuthStatus>('checking')
  const [authUser, setAuthUser] = useState<AuthUser | null>(null)
  const [displayName, setDisplayNameState] = useState<string>(
    () => loadProfile().displayName ?? STUB.displayName,
  )
  const [avatarTone, setAvatarToneState] = useState<AvatarTone>(
    () => loadProfile().avatarTone ?? 'gold',
  )

  // One-time session check on mount. getMe() never throws (network/4xx both
  // resolve to a normal AuthResult), so this can't leave authStatus stuck.
  useEffect(() => {
    let cancelled = false
    getMe().then((result) => {
      if (cancelled) return
      if (result.kind === 'ok') {
        setAuthUser(result.user)
        setAuthStatus('authenticated')
      } else {
        setAuthStatus('unauthenticated')
      }
    })
    return () => {
      cancelled = true
    }
  }, [])

  const signIn = useCallback((next: AuthUser) => {
    setAuthUser(next)
    setAuthStatus('authenticated')
  }, [])

  const signOut = useCallback(() => {
    void apiLogout()
    setAuthUser(null)
    setAuthStatus('unauthenticated')
  }, [])

  const setDisplayName = useCallback((name: string) => {
    setDisplayNameState(name)
    saveProfile({ displayName: name, avatarTone })
  }, [avatarTone])

  const setAvatarTone = useCallback((tone: AvatarTone) => {
    setAvatarToneState(tone)
    saveProfile({ displayName, avatarTone: tone })
  }, [displayName])

  const value = useMemo<CurrentUserContextValue>(() => {
    const role: UserRole = authUser?.role ?? 'player'
    const id = authUser?.email ?? 'guest'
    return {
      user: {
        id, displayName, initials: deriveInitials(displayName), avatarTone, role,
        signOut, editProfile: noop,
      },
      authStatus,
      signIn,
      setDisplayName,
      setAvatarTone,
    }
  }, [authStatus, authUser, displayName, avatarTone, signIn, signOut, setDisplayName, setAvatarTone])

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
