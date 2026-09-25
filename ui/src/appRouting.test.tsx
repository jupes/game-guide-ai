/**
 * appRouting.test.tsx -- agent-forge-harness-y40.
 *
 * App-level cases for the client router: a cold load at a deep path, a
 * link-carried single-use credential for the signed-in and signed-out cases,
 * and the identity-reset rule (R8) that must stop clobbering a deep link
 * without losing what it is for. Mounts `AppRoot` -- the SAME component
 * `main.tsx` renders -- so "the cold load works" is proven about the app
 * that ships (R13a), not a tower this test built for itself.
 *
 * A few R8-table rows (9, 12, 13) have no UI trigger in today's app
 * (`retryAuthCheck` is wired only to the `unavailable` screen's "Try again",
 * and Login is not rendered while already authenticated, so nothing can
 * call `signIn` a second time through the UI). Those use a small probe
 * alongside the REAL providers -- not a mock of them -- that calls the exact
 * same context methods a future caller would.
 */

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import { AppRoot } from './AppRoot'
import * as api from './api'
import { ThemeProvider } from './ds/theme'
import { AppNavProvider, useAppNav } from './shell/AppNav'
import { UrlNavigation } from './shell/UrlNavigation'
import { CurrentUserProvider, useCurrentUser } from './shell/currentUser'
import { ConversationStoreProvider } from './shell/ConversationStoreContext'
import { MemoryConversationStore } from './shell/conversationStore'

function storageValues(storage: Storage): string[] {
  const values: string[] = []
  for (let i = 0; i < storage.length; i += 1) {
    const key = storage.key(i)
    if (key !== null) values.push(storage.getItem(key) ?? '')
  }
  return values
}

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState({}, '', '/')
})

// ── A1 / R8 row 2 -- cold load of an allowlisted path ────────────────────────

describe('cold load', () => {
  it('a cold load of /profile renders Profile once the session check settles, without clobbering the URL', async () => {
    window.history.replaceState({}, '', '/profile')
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)

    expect(await screen.findByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/profile')
  })
})

// ── A9 -- signed-out vs signed-in for a link-carried token ───────────────────

describe('a link-carried token on a routed path', () => {
  it('(a) signed in: renders the screen and scrubs the token from history and storage', async () => {
    window.history.replaceState({}, '', '/profile#token=tok-secret')
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)

    expect(await screen.findByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/profile')
    expect(window.location.hash).toBe('')
    expect(storageValues(localStorage).some((v) => v.includes('tok-secret'))).toBe(false)
    expect(storageValues(sessionStorage).some((v) => v.includes('tok-secret'))).toBe(false)
  })

  it('(b) signed out: renders Login and still scrubs the token', async () => {
    window.history.replaceState({}, '', '/profile#token=tok-secret')
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'error', status: 401, message: 'not signed in',
    })

    render(<AppRoot />)

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
  })

  it('(c) root path with an invite, signed in: renders Landing and still scrubs it', async () => {
    window.history.replaceState({}, '', '/#invite=tok-secret')
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)

    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    expect(window.location.hash).toBe('')
    expect(storageValues(localStorage).some((v) => v.includes('tok-secret'))).toBe(false)
  })
})

// ── R8 -- the identity-reset rule, over AppRoot where the UI reaches it ─────

describe('identity reset (R8)', () => {
  it('row 7 -- signing in from Login lands on Landing at "/"', async () => {
    window.history.replaceState({}, '', '/')
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    vi.spyOn(api, 'login').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)

    await userEvent.type(await screen.findByLabelText(/email/i), 'ada@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'password123')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
  })

  it('row 8 (via the centralized 401 handler) -- a session that vanishes mid-session lands on Login at "/"', async () => {
    window.history.replaceState({}, '', '/')
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)
    // A cold load never lands ON the workspace (R6) -- reach it the same way
    // a real signed-in user would, by entering it from Landing. This also
    // exercises R4's `push` for a real navigation before the 401 reset below
    // exercises its `replace`.
    await userEvent.click(await screen.findByRole('button', { name: /enter the tavern/i }))
    expect(await screen.findByRole('main')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/workspace')

    await act(async () => {
      await api.postChat('hi', 'sage', null, (async () =>
        new Response(null, { status: 401 })) as typeof fetch)
    })

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument()
    await waitFor(() => expect(window.location.pathname).toBe('/'))
  })
})

// ── R8 rows with no UI trigger today: a probe calling the same context
//    methods a future caller would, alongside the REAL providers ──────────────

function IdentityProbe(): React.JSX.Element {
  const { conversationId, setConversationId, enterWorkspace } = useAppNav()
  const { user, retryAuthCheck, signIn } = useCurrentUser()
  return (
    <div>
      <p aria-label="conversation-id-probe">{conversationId ?? 'none'}</p>
      <button type="button" onClick={() => setConversationId('conv-1')}>
        Probe: seed conversation
      </button>
      <button type="button" onClick={() => enterWorkspace()}>Probe: enter workspace</button>
      <button type="button" onClick={retryAuthCheck}>Probe: retry</button>
      <button
        type="button"
        onClick={() => signIn({ email: 'bob@example.com', role: 'player' })}
      >
        Probe: sign in as bob
      </button>
      <button type="button" onClick={() => { void user.signOut() }}>Probe: sign out</button>
    </div>
  )
}

function renderProbedApp() {
  return render(
    <ThemeProvider>
      <AppNavProvider>
        <CurrentUserProvider>
          <ConversationStoreProvider store={new MemoryConversationStore()}>
            <UrlNavigation />
            <IdentityProbe />
            <App />
          </ConversationStoreProvider>
        </CurrentUserProvider>
      </AppNavProvider>
    </ThemeProvider>,
  )
}

describe('identity reset (R8) -- rows with no UI trigger today', () => {
  it('row 8 -- sign-out drops back to Login, replaces the URL with "/", and clears conversationId', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    vi.spyOn(api, 'logout').mockResolvedValue(true)
    renderProbedApp()
    await screen.findByText('Enter the Tavern')

    await userEvent.click(screen.getByRole('button', { name: 'Probe: seed conversation' }))
    expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('conv-1')

    // Leave '/' first (R4 pushes this), so the reset below actually has to
    // move the URL back -- otherwise push vs. replace is indistinguishable,
    // because there would be nothing to correct.
    await userEvent.click(screen.getByRole('button', { name: 'Probe: enter workspace' }))
    expect(window.location.pathname).toBe('/workspace')

    const historyLengthBeforeSignOut = window.history.length
    await userEvent.click(screen.getByRole('button', { name: 'Probe: sign out' }))

    // Exact match: the probe's OWN "Probe: sign in as bob" button also
    // contains the substring "sign in".
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument()
    expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('none')
    // Login appears as soon as authStatus flips; the identity effect and
    // UrlNavigation's own effect are a separate, later render pass, so wait
    // for the URL correction rather than reading it synchronously here.
    await waitFor(() => expect(window.location.pathname).toBe('/'))
    // The reset REPLACES rather than pushes: no new history entry, so a
    // signed-out user (or the next account) cannot Back into the previous
    // session's workspace.
    expect(window.history.length).toBe(historyLengthBeforeSignOut)
  })

  it('row 9 -- switching accounts while signed in still resets nav state', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    renderProbedApp()
    await screen.findByText('Enter the Tavern')

    await userEvent.click(screen.getByRole('button', { name: 'Probe: seed conversation' }))
    expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('conv-1')

    await userEvent.click(screen.getByRole('button', { name: 'Probe: sign in as bob' }))

    await waitFor(() =>
      expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('none'),
    )
  })

  it('rows 12-13 -- retrying while already signed in does not reset nav state', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    renderProbedApp()
    await screen.findByText('Enter the Tavern')

    await userEvent.click(screen.getByRole('button', { name: 'Probe: seed conversation' }))
    await userEvent.click(screen.getByRole('button', { name: 'Probe: retry' }))

    await waitFor(() => expect(api.getMe).toHaveBeenCalledTimes(2))
    expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('conv-1')
  })
})
