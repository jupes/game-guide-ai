import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import {
  CurrentUserProvider,
  useCurrentUser,
  STUB,
} from './currentUser'
import * as api from '../api'

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

  it('defaults to the guest STUB shape while the session check is pending', () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <CurrentUserProvider>{children}</CurrentUserProvider>
    )
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    // avatarTone defaults to STUB's 'gold'; initials are derived from the live
    // displayName ('Adventurer' -> 'A'), overriding STUB's static 'AV'. The
    // session check (getMe) is async, so immediately after render authStatus
    // is still 'checking' and the identity is still the guest default.
    expect(result.current.authStatus).toBe('checking')
    expect(result.current.user.displayName).toBe(STUB.displayName)
    expect(result.current.user.role).toBe('player')
    expect(result.current.user.initials).toBe('A')
  })
})

// ── x5bz.2 — session-backed identity (checking -> authenticated/unauthenticated) ──

describe('session check', () => {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <CurrentUserProvider>{children}</CurrentUserProvider>
  )

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('adopts the session when getMe resolves ok', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('authenticated'))
    expect(result.current.user.id).toBe('ada@example.com')
    expect(result.current.user.role).toBe('dm')
  })

  it('falls back to unauthenticated when getMe errors', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', message: 'not signed in' })
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('unauthenticated'))
  })

  it('signIn adopts an identity immediately (no re-fetch)', () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', message: 'not signed in' })
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    act(() => result.current.signIn({ email: 'bob@example.com', role: 'player' }))
    expect(result.current.authStatus).toBe('authenticated')
    expect(result.current.user.id).toBe('bob@example.com')
  })

  it('signOut calls the logout endpoint and reverts to unauthenticated', () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', message: 'not signed in' })
    const logoutSpy = vi.spyOn(api, 'logout').mockResolvedValue(undefined)
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    act(() => result.current.signIn({ email: 'ada@example.com', role: 'dm' }))
    act(() => result.current.user.signOut())
    expect(logoutSpy).toHaveBeenCalledTimes(1)
    expect(result.current.authStatus).toBe('unauthenticated')
    expect(result.current.user.id).toBe('guest')
  })
})

// ── swe1.7 — editable + persisted display name and avatar tone ────────────────
// jsdom's localStorage may not expose every method in the runner; use an
// in-memory stub so these tests are hermetic (mirrors conversationStore.test.ts).

describe('profile cosmetics (name + avatar tone)', () => {
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
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', message: 'not signed in' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

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
