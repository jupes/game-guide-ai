import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, render, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import appHtml from '../../index.html?raw'
import appCss from '../index.css?raw'
import {
  CurrentUserProvider,
  useCurrentUser,
  STUB,
} from './currentUser'
import * as api from '../api'
import { getGoogleOutcome, setGoogleOutcome } from './googleOutcome'
import { revealSignOutNotice } from './revealSignOut'

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
    revealSignOutNotice.clear()
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

  it('a 401 means signed out', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('unauthenticated'))
  })

  // ── An outage is not a logout ──────────────────────────────────────────────
  //
  // Only a 401 is the server saying the session is invalid. Anything else means
  // the question went unanswered, and sending that tester to Login would ask
  // them to re-enter credentials the backend cannot check.

  it.each([
    ['a 503 (backend down)', { kind: 'error' as const, status: 503, message: 'x' }],
    ['a 500', { kind: 'error' as const, status: 500, message: 'x' }],
    ['a 502 from a proxy', { kind: 'error' as const, status: 502, message: 'x' }],
    ['an edge 403 (Cloud Run IAM)', { kind: 'error' as const, status: 403, message: 'x' }],
    ['a network failure (no status)', { kind: 'error' as const, message: 'network error' }],
    ['an unreadable body (no status)', { kind: 'error' as const, message: 'unreadable' }],
  ])('reports %s as unavailable, not unauthenticated', async (_label, result) => {
    vi.spyOn(api, 'getMe').mockResolvedValue(result)
    const { result: hook } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(hook.current.authStatus).toBe('unavailable'))
    expect(hook.current.authStatus).not.toBe('unauthenticated')
  })

  it('retryAuthCheck re-runs the check and recovers when the backend returns', async () => {
    const getMe = vi.spyOn(api, 'getMe')
      .mockResolvedValueOnce({ kind: 'error', status: 503, message: 'down' })
      .mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })

    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('unavailable'))

    act(() => result.current.retryAuthCheck())
    await waitFor(() => expect(result.current.authStatus).toBe('authenticated'))
    expect(result.current.user.id).toBe('ada@example.com')
    expect(getMe).toHaveBeenCalledTimes(2)
  })

  it('an outage does not strand a stale identity in the context', async () => {
    // The session that WAS authenticated must not linger as an authenticated
    // identity behind an `unavailable` status — the workspace is gated on the
    // status, and a half-signed-in state is the kind of thing that leaks one
    // user's conversation list to the next.
    vi.spyOn(api, 'getMe')
      .mockResolvedValueOnce({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
      .mockResolvedValue({ kind: 'error', status: 503, message: 'down' })

    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('authenticated'))

    act(() => result.current.retryAuthCheck())
    await waitFor(() => expect(result.current.authStatus).toBe('unavailable'))
    expect(result.current.user.id).toBe('guest')
    expect(result.current.user.role).toBe('player')
  })

  it('signIn adopts an identity immediately (no re-fetch)', () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    act(() => result.current.signIn({ email: 'bob@example.com', role: 'player' }))
    expect(result.current.authStatus).toBe('authenticated')
    expect(result.current.user.id).toBe('bob@example.com')
  })

  it('signIn on ANY path (Signup too) clears the "table can still see" notice, so another account never reads it (REVEAL-16)', () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    revealSignOutNotice.set(['Ondrey'])
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    act(() => result.current.signIn({ email: 'new@example.com', role: 'player' }))
    expect(revealSignOutNotice.text).toBeNull()
  })

  it('signOut calls the logout endpoint and reverts to unauthenticated', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
    const logoutSpy = vi.spyOn(api, 'logout').mockResolvedValue(true)
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('authenticated'))
    setGoogleOutcome({ search: '?google=linked', pathname: '/profile' })
    revealSignOutNotice.set(['Private NPC name'])
    act(() => result.current.setDisplayName('Private alias'))
    act(() => result.current.setAvatarTone('arcane'))
    localStorage.setItem('game-guide-ai:gm-pending-stops:ada@example.com', 'command')
    sessionStorage.setItem('rag-chat:conversations', 'legacy')
    await act(async () => {
      await result.current.user.signOut()
    })
    expect(logoutSpy).toHaveBeenCalledTimes(1)
    expect(result.current.authStatus).toBe('unauthenticated')
    expect(result.current.user.id).toBe('guest')
    expect(getGoogleOutcome()).toBeNull()
    expect(revealSignOutNotice.text).toBeNull()
    expect(localStorage.getItem('game-guide-ai:gm-pending-stops:ada@example.com')).toBeNull()
    expect(sessionStorage.getItem('rag-chat:conversations')).toBeNull()
    act(() => result.current.signIn({ email: 'ada@example.com', role: 'dm' }))
    expect(result.current.user.displayName).toBe(STUB.displayName)
    expect(result.current.user.avatarTone).toBe('gold')
  })

  it('a FAILED logout keeps the user signed in (the cookie is still live)', async () => {
    // Resolve the mount session check as authenticated first — otherwise it
    // settles later and clobbers the signed-in state this test is asserting on.
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    vi.spyOn(api, 'logout').mockResolvedValue(false)
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('authenticated'))

    const key = 'game-guide-ai:gm-pending-stops:ada@example.com'
    localStorage.setItem(key, 'command')
    let outcome: boolean | undefined
    await act(async () => {
      outcome = await result.current.user.signOut()
    })
    // Only the server can clear an httpOnly cookie — pretending here would show
    // "signed out" while a refresh silently restores the session.
    expect(outcome).toBe(false)
    expect(result.current.authStatus).toBe('authenticated')
    expect(result.current.user.id).toBe('ada@example.com')
    expect(localStorage.getItem(key)).toBe('command')
    localStorage.removeItem(key)
  })

  it('a 401 from any guarded call drops the app back to unauthenticated', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.authStatus).toBe('authenticated'))

    const key = 'game-guide-ai:gm-pending-stops:ada@example.com'
    localStorage.setItem(key, 'command')
    revealSignOutNotice.set(['Private NPC name'])
    // Simulate a guarded endpoint reporting the session is gone.
    await act(async () => {
      const res = await api.postChat('hi', 'sage', null, (async () =>
        new Response(null, { status: 401 })) as typeof fetch)
      expect(res.kind).toBe('error')
    })
    expect(result.current.authStatus).toBe('unauthenticated')
    expect(localStorage.getItem(key)).toBe('command')
    expect(revealSignOutNotice.text).toContain('Private NPC name')
    localStorage.removeItem(key)
  })

  it.each(['still signed in', 'signed out', 'unavailable'] as const)('checks the current cookie after a raced sign-in: %s', async outcome => {
    const ada = { email: 'ada@example.com', role: 'dm' as const }
    const bea = { email: 'bea@example.com', role: 'player' as const }
    const me = vi.spyOn(api, 'getMe').mockResolvedValueOnce({ kind: 'ok', user: ada })
      .mockResolvedValue(outcome === 'still signed in' ? { kind: 'ok', user: bea }
        : { kind: 'error', status: outcome === 'signed out' ? 401 : 503, message: 'Could not load account' })
    let finish!: (ok: boolean) => void
    vi.spyOn(api, 'logout').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const channel = { onmessage: null, close: vi.fn(), postMessage: vi.fn() }
    const { result } = renderHook(() => useCurrentUser(), {
      wrapper: ({ children }) => <CurrentUserProvider identityChannelFactory={() => channel}>{children}</CurrentUserProvider>,
    })
    await waitFor(() => expect(result.current.authStatus).toBe('authenticated'))
    const pending = result.current.user.signOut()
    act(() => result.current.signIn(bea))
    act(() => result.current.setDisplayName('Bea'))
    await act(async () => {
      finish(true)
      await expect(pending).resolves.toBe(outcome === 'signed out')
    })
    expect(me).toHaveBeenCalledTimes(2)
    expect(channel.postMessage).toHaveBeenCalledTimes(2)
    const key = 'game-guide-ai:profile:bea@example.com'
    expect(result.current.authStatus).toBe(outcome === 'still signed in' ? 'authenticated'
      : outcome === 'signed out' ? 'unauthenticated' : 'unavailable')
    if (outcome === 'still signed in') {
      expect(result.current.user.id).toBe(bea.email)
      expect(result.current.user.displayName).toBe('Bea')
    } else expect(result.current.user.id).toBe('guest')
    if (outcome === 'signed out') expect(localStorage.getItem(key)).toBeNull()
    else expect(localStorage.getItem(key)).toContain('Bea')
    localStorage.removeItem(key)
  })

  it.each(['same account', 'different account', 'signed out', 'unavailable'] as const)(
    'covers browser history snapshots until current identity is verified: %s', async outcome => {
      const ada = { email: 'ada@example.com', role: 'dm' as const }
      const bea = { email: 'bea@example.com', role: 'player' as const }
      let settle!: (current: api.AuthResult) => void
      const me = vi.spyOn(api, 'getMe').mockResolvedValueOnce({ kind: 'ok', user: ada })
        .mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
      function AccountView() {
        const { user, authStatus } = useCurrentUser()
        return authStatus === 'authenticated' ? <>
          <label>{user.id}<input key={user.id} /></label>
          {createPortal(<aside aria-label="Identity details">{user.id}</aside>, document.body)}
        </> : <p>{authStatus}</p>
      }
      const root = document.createElement('div')
      root.id = 'root'
      const status = new DOMParser().parseFromString(appHtml, 'text/html').getElementById('session-restore-status')!
      const styles = document.createElement('style')
      styles.textContent = appCss
      document.head.append(styles)
      document.body.append(status, root)
      const view = render(<CurrentUserProvider identityChannelFactory={() => null}><AccountView /></CurrentUserProvider>, { container: root })
      try {
        const draft = await view.findByRole('textbox') as HTMLInputElement
        const portal = document.body.querySelector('aside')!
        expect(root.contains(portal)).toBe(false)
        expect(status).not.toBeVisible()
        draft.value = 'Unsaved private draft'
        act(() => window.dispatchEvent(new PageTransitionEvent('pagehide')))
        expect(root).toBeVisible()
        act(() => window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })))
        expect(root).not.toBeVisible()
        expect(portal).not.toBeVisible()
        expect(getComputedStyle(root).display).toBe('none')
        expect(getComputedStyle(portal).display).toBe('none')
        expect(status).toBeVisible()
        expect(status).toHaveRole('status')
        expect(status).toHaveTextContent('Loading…')
        act(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })))
        expect(root).not.toBeVisible()
        expect(portal).not.toBeVisible()
        expect(status).toBeVisible()
        expect(me).toHaveBeenCalledTimes(2)
        await act(async () => {
          if (outcome === 'same account' || outcome === 'different account') {
            settle({ kind: 'ok', user: outcome === 'same account' ? ada : bea })
          } else settle({ kind: 'error', status: outcome === 'signed out' ? 401 : 503, message: 'Session check failed' })
        })
        expect(root).toBeVisible()
        expect(status).not.toBeVisible()
        if (outcome === 'same account') expect(view.getByRole('textbox')).toHaveValue('Unsaved private draft')
        else {
          expect(draft).not.toBeInTheDocument()
          expect(root).not.toHaveTextContent(ada.email)
          expect(root).toHaveTextContent(outcome === 'different account' ? bea.email : outcome === 'signed out' ? 'unauthenticated' : 'unavailable')
        }
        act(() => window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })))
        expect(status).toBeVisible()
      } finally {
        view.unmount()
        expect(status).not.toBeVisible()
        expect(document.documentElement).not.toHaveAttribute('data-session-restoring')
        root.remove()
        status.remove()
        styles.remove()
      }
    },
  )
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
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
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

  it('adopts a pre-auth profile for the first real identity, and consumes it', async () => {
    // Existing users stored cosmetics under the un-namespaced key; without a
    // migration they'd silently lose their name and avatar after this deploys.
    lsMock.setItem(
      'game-guide-ai:profile',
      JSON.stringify({ displayName: 'Astra Vail', avatarTone: 'arcane' }),
    )
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'astra@example.com', role: 'dm' },
    })

    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    await waitFor(() => expect(result.current.user.displayName).toBe('Astra Vail'))
    expect(result.current.user.avatarTone).toBe('arcane')
    // Consumed, so a second account can't inherit the same profile.
    expect(lsMock.getItem('game-guide-ai:profile')).toBeNull()
    expect(lsMock.getItem('game-guide-ai:profile:astra@example.com')).not.toBeNull()
  })

  it('does not consume the pre-auth profile into the guest bucket', () => {
    lsMock.setItem('game-guide-ai:profile', JSON.stringify({ displayName: 'Astra Vail' }))
    // Still 'checking' → identity is guest; migrating now would strand it.
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    expect(result.current.user.displayName).toBe('Adventurer')
    expect(lsMock.getItem('game-guide-ai:profile')).not.toBeNull()
  })
})
