/**
 * currentUser — Stub current user context.
 *
 * Provides a guest "Adventurer" stub for the shell during development, plus
 * the DM/player role (channel-chats CP-D): a localStorage-persisted toggle
 * that gates the GM channel in the UI. This is honest-scope gating only — the
 * server does not enforce roles until real auth exists. In a real app,
 * replace STUB with a real auth integration.
 */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import * as React from 'react'
import { deriveInitials, type AvatarTone } from '../ds/Avatar'

// ── Types ─────────────────────────────────────────────────────────────────────

export type UserRole = 'dm' | 'player'

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
  setRole: (role: UserRole) => void
  setDisplayName: (name: string) => void
  setAvatarTone: (tone: AvatarTone) => void
}

// ── Stub ──────────────────────────────────────────────────────────────────────

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

// ── Role persistence (guarded, matching conversationStore's posture) ──────────

const ROLE_STORAGE_KEY = 'game-guide-ai:role'

function loadRole(): UserRole {
  try {
    return localStorage.getItem(ROLE_STORAGE_KEY) === 'dm' ? 'dm' : 'player'
  } catch {
    // localStorage unavailable (privacy mode, SSR) — least-privileged default.
    return 'player'
  }
}

function saveRole(role: UserRole): void {
  try {
    localStorage.setItem(ROLE_STORAGE_KEY, role)
  } catch (err) {
    console.warn('currentUser: could not persist role', err)
  }
}

// ── Profile persistence (name + avatar tone; role stays under its own key) ────
// Local-stub only — real per-user profiles arrive with the pilot-auth work (x5bz.2).

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
  const [role, setRoleState] = useState<UserRole>(loadRole)
  const [displayName, setDisplayNameState] = useState<string>(
    () => loadProfile().displayName ?? STUB.displayName,
  )
  const [avatarTone, setAvatarToneState] = useState<AvatarTone>(
    () => loadProfile().avatarTone ?? 'gold',
  )

  const setRole = useCallback((next: UserRole) => {
    setRoleState(next)
    saveRole(next)
  }, [])

  const setDisplayName = useCallback((name: string) => {
    setDisplayNameState(name)
    saveProfile({ displayName: name, avatarTone })
  }, [avatarTone])

  const setAvatarTone = useCallback((tone: AvatarTone) => {
    setAvatarToneState(tone)
    saveProfile({ displayName, avatarTone: tone })
  }, [displayName])

  const value = useMemo<CurrentUserContextValue>(
    () => ({
      user: { ...STUB, displayName, initials: deriveInitials(displayName), avatarTone, role },
      setRole,
      setDisplayName,
      setAvatarTone,
    }),
    [displayName, avatarTone, role, setRole, setDisplayName, setAvatarTone],
  )
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
