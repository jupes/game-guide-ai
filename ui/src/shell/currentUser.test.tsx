import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import {
  CurrentUserProvider,
  useCurrentUser,
  STUB,
} from './currentUser'

// ── 02t.6 — useCurrentUser provider guard (matches useTheme's pattern) ─────────

describe('useCurrentUser', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('throws a helpful error when used outside a <CurrentUserProvider>', () => {
    // React logs the thrown error to console.error during render — silence it.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => renderHook(() => useCurrentUser())).toThrow(/CurrentUserProvider/)
  })

  it('returns the provided user inside a <CurrentUserProvider>', () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <CurrentUserProvider>{children}</CurrentUserProvider>
    )
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    // avatarTone defaults to STUB's 'gold'; initials are derived from the live
    // displayName ('Adventurer' -> 'A'), overriding STUB's static 'AV'.
    expect(result.current.user).toEqual({ ...STUB, role: 'player', initials: 'A' })
  })
})

// ── channel-chats CP-D — DM/player role on the current user ───────────────────
// jsdom 29's localStorage may not expose every method in the runner; use an
// in-memory stub so these tests are hermetic (mirrors conversationStore.test.ts).

describe('user role', () => {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <CurrentUserProvider>{children}</CurrentUserProvider>
  )

  function makeLocalStorageStub() {
    let store: Record<string, string> = {}
    return {
      getItem: (key: string) => store[key] ?? null,
      setItem: (key: string, value: string) => { store[key] = value },
      removeItem: (key: string) => { delete store[key] },
      clear: () => { store = {} },
      get length() { return Object.keys(store).length },
      key: (index: number) => Object.keys(store)[index] ?? null,
    }
  }
  let lsMock: ReturnType<typeof makeLocalStorageStub>

  beforeEach(() => {
    lsMock = makeLocalStorageStub()
    vi.stubGlobal('localStorage', lsMock)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('defaults to player', () => {
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    expect(result.current.user.role).toBe('player')
  })

  it('setRole updates the role and persists it', () => {
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    act(() => result.current.setRole('dm'))
    expect(result.current.user.role).toBe('dm')
    expect(localStorage.getItem('game-guide-ai:role')).toBe('dm')
  })

  it('seeds the role from localStorage (survives reload)', () => {
    localStorage.setItem('game-guide-ai:role', 'dm')
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    expect(result.current.user.role).toBe('dm')
  })

  it('falls back to player on an unrecognized stored value', () => {
    localStorage.setItem('game-guide-ai:role', 'archlich')
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    expect(result.current.user.role).toBe('player')
  })

  // ── swe1.7 — editable + persisted display name and avatar tone ──────────────

  it('setDisplayName updates the name and persists across a fresh provider', () => {
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    act(() => result.current.setDisplayName('Astra Vail'))
    expect(result.current.user.displayName).toBe('Astra Vail')
    expect(result.current.user.initials).toBe('AV') // derived from the new name
    const again = renderHook(() => useCurrentUser(), { wrapper })
    expect(again.result.current.user.displayName).toBe('Astra Vail')
  })

  it('setAvatarTone updates the tone and persists it; default is gold', () => {
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    expect(result.current.user.avatarTone).toBe('gold')
    act(() => result.current.setAvatarTone('arcane'))
    expect(result.current.user.avatarTone).toBe('arcane')
    const again = renderHook(() => useCurrentUser(), { wrapper })
    expect(again.result.current.user.avatarTone).toBe('arcane')
  })
})
