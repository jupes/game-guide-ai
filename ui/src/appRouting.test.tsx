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
 * A few R8-table rows (9, 12, 13, 14) have no UI trigger in today's app
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

/** Let every commit already queued land before reading the screen or the URL
 * again. What a test waits for renders in the SAME commit the session check
 * settles in; a reset that wrongly fired on that commit -- and
 * UrlNavigation's URL write after it -- would only land in LATER commits, so
 * reading `location` straight after `findBy*` races them. */
async function flushQueuedCommits(): Promise<void> {
  await act(async () => {})
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
    // The heading appearing means getMe has settled. Row 2 is the FIRST
    // settled status, so nothing may reset; flush, then look again.
    await flushQueuedCommits()
    expect(screen.getByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/profile')
  })
})

// ── A9 -- signed-out vs signed-in for a link-carried token ───────────────────

describe('a link-carried token on a routed path', () => {
  it('(a) signed in: renders the screen and scrubs the token from history and storage', async () => {
    window.history.replaceState({}, '', '/profile#token=tok-secret')
    const historyLengthAtBoot = window.history.length
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)

    expect(await screen.findByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    await flushQueuedCommits()
    expect(screen.getByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/profile')
    expect(window.location.hash).toBe('')
    // The scrub REPLACES: a pushed entry would leave `#token=` one Back away.
    expect(window.history.length).toBe(historyLengthAtBoot)
    expect(storageValues(localStorage).some((v) => v.includes('tok-secret'))).toBe(false)
    expect(storageValues(sessionStorage).some((v) => v.includes('tok-secret'))).toBe(false)
  })

  it('(b) signed out: renders Login and still scrubs the token', async () => {
    window.history.replaceState({}, '', '/profile#token=tok-secret')
    const historyLengthAtBoot = window.history.length
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'error', status: 401, message: 'not signed in',
    })

    render(<AppRoot />)

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
    expect(window.history.length).toBe(historyLengthAtBoot)
  })

  it('(c) root path with an invite, signed in: renders Landing and still scrubs it', async () => {
    window.history.replaceState({}, '', '/#invite=tok-secret')
    const historyLengthAtBoot = window.history.length
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)

    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await flushQueuedCommits()
    expect(window.location.hash).toBe('')
    expect(window.history.length).toBe(historyLengthAtBoot)
    expect(storageValues(localStorage).some((v) => v.includes('tok-secret'))).toBe(false)
  })

  // R7 scrubs on ANY path: a path the boot correction sends to '/' must not
  // carry the token across with it.
  it.each(['/nope', '/workspace'])(
    '(d) %s#token=: the boot correction lands on "/" with the token already gone',
    async (path) => {
      window.history.replaceState({}, '', `${path}#token=tok-secret`)
      const historyLengthAtBoot = window.history.length
      vi.spyOn(api, 'getMe').mockResolvedValue({
        kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
      })

      render(<AppRoot />)

      expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
      await flushQueuedCommits()
      expect(window.location.pathname).toBe('/')
      expect(window.location.hash).toBe('')
      expect(window.history.length).toBe(historyLengthAtBoot)
      expect(storageValues(localStorage).some((v) => v.includes('tok-secret'))).toBe(false)
      expect(storageValues(sessionStorage).some((v) => v.includes('tok-secret'))).toBe(false)
    },
  )
})

// ── R8 -- the identity-reset rule, over AppRoot where the UI reaches it ─────

describe('identity reset (R8)', () => {
  it('row 7 -- signing in from Login on a cold-loaded /profile lands on Landing at "/"', async () => {
    window.history.replaceState({}, '', '/profile')
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    vi.spyOn(api, 'login').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })

    render(<AppRoot />)

    await userEvent.type(await screen.findByLabelText(/email/i), 'ada@example.com')
    // Row 3: the first settled status (signed out) resets nothing, so the
    // deep link is still there under Login.
    expect(window.location.pathname).toBe('/profile')
    await userEvent.type(screen.getByLabelText(/password/i), 'password123')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    // The user lands on Landing rather than the Profile they deep-linked to
    // (carrying a route across a login is a non-goal). Login's own submit
    // handler also calls `backToLanding()` -- a push, the user's own
    // navigation -- so this path cannot tell whether the RESET ran; the
    // probe's row-7 case below signs in with no caller-side navigation.
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await waitFor(() => expect(window.location.pathname).toBe('/'))
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

/** Signed in as ada, with a conversation open, on the workspace at
 * `/workspace` -- so a reset has a conversation, a screen AND a URL to
 * correct. Returns `history.length` at that point. */
async function seedAdaOnTheWorkspace(): Promise<number> {
  await screen.findByText('Enter the Tavern')
  await userEvent.click(screen.getByRole('button', { name: 'Probe: seed conversation' }))
  expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('conv-1')
  await userEvent.click(screen.getByRole('button', { name: 'Probe: enter workspace' }))
  expect(window.location.pathname).toBe('/workspace')
  return window.history.length
}

describe('identity reset (R8) -- rows with no UI trigger today', () => {
  it('row 7 -- a sign-in whose caller does not navigate still resets nav state and replaces the URL with "/"', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    renderProbedApp()
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument()

    // Signed out, with navigation state a sign-in must not carry over.
    await userEvent.click(screen.getByRole('button', { name: 'Probe: seed conversation' }))
    await userEvent.click(screen.getByRole('button', { name: 'Probe: enter workspace' }))
    expect(window.location.pathname).toBe('/workspace')
    const historyLengthBeforeSignIn = window.history.length

    // unauthenticated -> authenticated, 'guest' -> bob: a real sign-in.
    await userEvent.click(screen.getByRole('button', { name: 'Probe: sign in as bob' }))

    await waitFor(() =>
      expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('none'),
    )
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await waitFor(() => expect(window.location.pathname).toBe('/'))
    expect(window.history.length).toBe(historyLengthBeforeSignIn)
  })

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

  it('row 9 -- switching accounts while signed in resets nav state and replaces the URL with "/"', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    renderProbedApp()
    const historyLengthBeforeSwitch = await seedAdaOnTheWorkspace()

    await userEvent.click(screen.getByRole('button', { name: 'Probe: sign in as bob' }))

    await waitFor(() =>
      expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('none'),
    )
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await waitFor(() => expect(window.location.pathname).toBe('/'))
    expect(window.history.length).toBe(historyLengthBeforeSwitch)
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

  it('row 14 -- a re-check that finds the session gone resets nav state and replaces the URL with "/"', async () => {
    vi.spyOn(api, 'getMe')
      .mockResolvedValueOnce({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
      .mockResolvedValueOnce({ kind: 'error', status: 401, message: 'session gone' })
    renderProbedApp()
    const historyLengthBeforeRecheck = await seedAdaOnTheWorkspace()

    // authenticated -> checking (row 12: `user.id` stays ada's) ->
    // unauthenticated. `checking` must not erase the last SETTLED id, or
    // this real sign-out would reset nothing.
    await userEvent.click(screen.getByRole('button', { name: 'Probe: retry' }))

    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('none'),
    )
    await waitFor(() => expect(window.location.pathname).toBe('/'))
    expect(window.history.length).toBe(historyLengthBeforeRecheck)
    expect(api.getMe).toHaveBeenCalledTimes(2)
  })

  it('re-check account switch (ada -> checking -> bob) -- bob inherits neither the conversation nor the screen', async () => {
    vi.spyOn(api, 'getMe')
      .mockResolvedValueOnce({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
      .mockResolvedValueOnce({ kind: 'ok', user: { email: 'bob@example.com', role: 'player' } })
    renderProbedApp()
    const historyLengthBeforeRecheck = await seedAdaOnTheWorkspace()

    await userEvent.click(screen.getByRole('button', { name: 'Probe: retry' }))

    await waitFor(() => expect(api.getMe).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.getByLabelText('conversation-id-probe')).toHaveTextContent('none'),
    )
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await waitFor(() => expect(window.location.pathname).toBe('/'))
    expect(window.history.length).toBe(historyLengthBeforeRecheck)
  })
})
