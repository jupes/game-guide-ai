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
import {
  getMe,
  logout as apiLogout,
  setUnauthorizedHandler,
  type AuthUser,
} from '../api'

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
  /** Resolves false when the SERVER refused to end the session — the caller
   * must keep showing the user as signed in (the httpOnly cookie is still live). */
  signOut(): Promise<boolean>
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
  signOut: async () => true,
  editProfile: noop,
}

// ── Profile persistence (local-stub only; unrelated to the server session) ───
// Real per-user profile storage is out of scope for the pilot.

// Namespaced per account: a single shared key leaked one user's display name and
// avatar to the next person to sign in on the same browser. Signed-out/unknown
// users get their own bucket rather than the previous user's.
function profileStorageKey(userId: string): string {
  return `game-guide-ai:profile:${userId}`
}

/** The un-namespaced key this replaced. Migrated onto the first REAL identity
 * that loads (never `guest`, which would strand it while the session check is
 * still in flight) and consumed, so a second account can't inherit it too. */
const PRE_AUTH_PROFILE_KEY = 'game-guide-ai:profile'
const GUEST_USER_ID = 'guest'

const AVATAR_TONES: readonly AvatarTone[] = ['gold', 'ember', 'verdigris', 'arcane']

interface StoredProfile {
  displayName?: string
  avatarTone?: AvatarTone
}

function isAvatarTone(value: unknown): value is AvatarTone {
  return typeof value === 'string' && (AVATAR_TONES as readonly string[]).includes(value)
}

/** Move a pre-auth profile onto this account's key, once. Returns its payload so
 * the caller can use it immediately. */
function migratePreAuthProfile(userId: string): string | null {
  if (userId === GUEST_USER_ID) return null
  const legacy = localStorage.getItem(PRE_AUTH_PROFILE_KEY)
  if (legacy === null) return null
  try {
    localStorage.setItem(profileStorageKey(userId), legacy)
    localStorage.removeItem(PRE_AUTH_PROFILE_KEY)
  } catch {
    // Quota/availability errors: still return it so this session reads it.
  }
  return legacy
}

function loadProfile(userId: string): StoredProfile {
  try {
    const raw = localStorage.getItem(profileStorageKey(userId))
      ?? migratePreAuthProfile(userId)
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

function saveProfile(userId: string, profile: StoredProfile): void {
  try {
    localStorage.setItem(profileStorageKey(userId), JSON.stringify(profile))
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
  const userId = authUser?.email ?? 'guest'
  // Edits made this session, keyed by account. The stored profile is READ per
  // identity rather than mirrored into state, so switching accounts can't leave
  // the previous user's name/avatar on screen (and needs no syncing effect).
  const [edits, setEdits] = useState<Record<string, StoredProfile>>({})

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

  // Centralized 401: any guarded call that finds the session gone drops the app
  // back to Login, instead of trapping the user in an authenticated-looking
  // shell where every request fails.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuthUser(null)
      setAuthStatus('unauthenticated')
    })
    return () => setUnauthorizedHandler(null)
  }, [])

  const signIn = useCallback((next: AuthUser) => {
    setAuthUser(next)
    setAuthStatus('authenticated')
  }, [])

  const signOut = useCallback(async (): Promise<boolean> => {
    // Only the server can clear an httpOnly cookie. If it refuses, stay signed
    // in rather than pretending — otherwise a refresh silently restores the
    // session the user believes they ended.
    const ok = await apiLogout()
    if (!ok) return false
    setAuthUser(null)
    setAuthStatus('unauthenticated')
    return true
  }, [])

  const profile = useMemo(
    () => edits[userId] ?? loadProfile(userId),
    [edits, userId],
  )
  const displayName = profile.displayName ?? STUB.displayName
  const avatarTone = profile.avatarTone ?? 'gold'

  const updateProfile = useCallback((patch: StoredProfile) => {
    setEdits((prev) => {
      const next = { ...(prev[userId] ?? loadProfile(userId)), ...patch }
      saveProfile(userId, next)
      return { ...prev, [userId]: next }
    })
  }, [userId])

  const setDisplayName = useCallback((name: string) => {
    updateProfile({ displayName: name })
  }, [updateProfile])

  const setAvatarTone = useCallback((tone: AvatarTone) => {
    updateProfile({ avatarTone: tone })
  }, [updateProfile])

  const value = useMemo<CurrentUserContextValue>(() => {
    const role: UserRole = authUser?.role ?? 'player'
    return {
      user: {
        id: userId, displayName, initials: deriveInitials(displayName), avatarTone, role,
        signOut, editProfile: noop,
      },
      authStatus,
      signIn,
      setDisplayName,
      setAvatarTone,
    }
  }, [authStatus, authUser, userId, displayName, avatarTone, signIn, signOut, setDisplayName, setAvatarTone])

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
