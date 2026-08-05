/**
 * Behavior #9 (x5bz.2 Checkpoint E) — frontend auth.
 *
 * Login + Signup screens submit to the API and adopt the returned session;
 * App gates on authStatus, showing Signup for an invite deep-link
 * (`/#invite=<token>` — the token rides in the FRAGMENT so it never reaches the
 * server or its request logs) and Login otherwise.
 */

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import App from '../App'
import * as api from '../api'
import { ThemeProvider } from '../ds/theme'
import { AppNavProvider } from './AppNav'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { CurrentUserContext, CurrentUserProvider } from './currentUser'
import type { CurrentUserContextValue, AuthStatus } from './currentUser'
import { Login } from './Login'
import { Signup } from './Signup'

function makeUserState(
  overrides: Partial<CurrentUserContextValue> = {},
): CurrentUserContextValue {
  return {
    user: {
      id: 'guest',
      displayName: 'Adventurer',
      initials: 'AV',
      role: 'player',
      signOut: vi.fn(),
      editProfile: vi.fn(),
    },
    authStatus: 'authenticated',
    retryAuthCheck: vi.fn(),
    signIn: vi.fn(),
    setDisplayName: vi.fn(),
    setAvatarTone: vi.fn(),
    ...overrides,
  }
}

function withProviders(children: ReactNode, user: CurrentUserContextValue) {
  return (
    <ThemeProvider>
      <AppNavProvider>
        <CurrentUserContext.Provider value={user}>
          <ConversationStoreProvider store={new MemoryConversationStore()}>
            {children}
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavProvider>
    </ThemeProvider>
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState({}, '', '/')
})

// ── Login screen ──────────────────────────────────────────────────────────────

describe('Login screen', () => {
  it('signs in with email + password and adopts the session', async () => {
    const signIn = vi.fn()
    vi.spyOn(api, 'login').mockResolvedValue({
      kind: 'ok', user: { email: 'ada@example.com', role: 'dm' },
    })
    render(withProviders(<Login />, makeUserState({ signIn })))

    await userEvent.type(screen.getByLabelText(/email/i), 'ada@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'password123')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() =>
      expect(signIn).toHaveBeenCalledWith({ email: 'ada@example.com', role: 'dm' }),
    )
    expect(api.login).toHaveBeenCalledWith('ada@example.com', 'password123')
  })

  it('shows the error message on bad credentials and does not sign in', async () => {
    const signIn = vi.fn()
    vi.spyOn(api, 'login').mockResolvedValue({
      kind: 'error', status: 401, message: 'invalid email or password',
    })
    render(withProviders(<Login />, makeUserState({ signIn })))

    await userEvent.type(screen.getByLabelText(/email/i), 'ada@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'nope')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid email or password/i)
    expect(signIn).not.toHaveBeenCalled()
  })
})

// ── Signup screen ─────────────────────────────────────────────────────────────

describe('Signup screen', () => {
  it('redeems the invite it was given and adopts the session', async () => {
    const signIn = vi.fn()
    vi.spyOn(api, 'signup').mockResolvedValue({
      kind: 'ok', user: { email: 'new@example.com', role: 'player' },
    })
    render(withProviders(<Signup invite="tok-abc" onUseLogin={vi.fn()} />, makeUserState({ signIn })))

    await userEvent.type(screen.getByLabelText(/email/i), 'new@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'password123')
    await userEvent.click(screen.getByRole('button', { name: /create account/i }))

    await waitFor(() =>
      expect(api.signup).toHaveBeenCalledWith('new@example.com', 'password123', 'tok-abc'),
    )
    expect(signIn).toHaveBeenCalledWith({ email: 'new@example.com', role: 'player' })
  })

  it('surfaces a used/expired invite error', async () => {
    vi.spyOn(api, 'signup').mockResolvedValue({
      kind: 'error', status: 400, message: 'This invite link has already been used.',
    })
    render(withProviders(<Signup invite="used" onUseLogin={vi.fn()} />, makeUserState()))

    await userEvent.type(screen.getByLabelText(/email/i), 'a@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'password123')
    await userEvent.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/already been used/i)
  })
})

// ── App auth gate ─────────────────────────────────────────────────────────────

describe('App auth gate', () => {
  function renderApp(authStatus: AuthStatus) {
    return render(withProviders(<App />, makeUserState({ authStatus })))
  }

  it('shows Login when unauthenticated with no invite in the URL', () => {
    renderApp('unauthenticated')
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument()
  })

  it('shows Signup when the URL carries an invite token', () => {
    window.history.replaceState({}, '', '/#invite=tok-abc')
    renderApp('unauthenticated')
    expect(screen.getByRole('button', { name: /create account/i })).toBeInTheDocument()
  })

  it('shows the app (not Login) once authenticated', () => {
    renderApp('authenticated')
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument()
    expect(screen.getByText('Enter the Tavern')).toBeInTheDocument()
  })

  it('holds a neutral loading state while the session check is pending', () => {
    renderApp('checking')
    // Neither Login (no flash at a signed-in tester) NOR the workspace: letting
    // the user start a conversation before their identity is known stranded it
    // in the guest store when the real identity arrived.
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument()
    expect(screen.queryByText('Enter the Tavern')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/loading/i)
  })

  it('scrubs the single-use invite from the URL immediately', async () => {
    window.history.replaceState({}, '', '/#invite=tok-abc')
    renderApp('unauthenticated')
    // The token is a credential: leaving it in the address bar keeps it in
    // history, and makes a later sign-out land on Signup with a spent invite.
    expect(window.location.hash).toBe('')
    expect(screen.getByRole('button', { name: /create account/i })).toBeInTheDocument()
  })

  it('offers a route to Login from the Signup screen', async () => {
    window.history.replaceState({}, '', '/#invite=tok-abc')
    renderApp('unauthenticated')
    await userEvent.click(screen.getByRole('button', { name: /already have an account/i }))
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument()
  })

  it('rejects a too-short password client-side, without calling the API', async () => {
    const signupSpy = vi.spyOn(api, 'signup')
    render(withProviders(<Signup invite="tok" onUseLogin={vi.fn()} />, makeUserState()))

    await userEvent.type(screen.getByLabelText(/email/i), 'a@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'short')
    await userEvent.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/at least 8 characters/i)
    expect(signupSpy).not.toHaveBeenCalled()
  })

  it('a 401 from the session check lands the user on Login (real provider)', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    render(realApp())
    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument()
  })
})

// ── An outage is not a logout (PR #43 review) ────────────────────────────────

function realApp() {
  return (
    <ThemeProvider>
      <AppNavProvider>
        <CurrentUserProvider>
          <ConversationStoreProvider store={new MemoryConversationStore()}>
            <App />
          </ConversationStoreProvider>
        </CurrentUserProvider>
      </AppNavProvider>
    </ThemeProvider>
  )
}

describe('session check outage', () => {
  it('a 503 shows a retry, NOT the login form', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 503, message: 'down' })
    render(realApp())

    expect(await screen.findByRole('button', { name: /try again/i })).toBeInTheDocument()
    // The regression this pins: a backend blip used to present the login form,
    // where every credential the tester typed would fail for the same reason.
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument()
  })

  it('announces the failure to assistive tech', async () => {
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', message: 'network error' })
    render(realApp())
    expect(await screen.findByRole('alert')).toHaveTextContent(/can.t reach the service/i)
  })

  it('Try again re-checks and lets the user straight back in', async () => {
    vi.spyOn(api, 'getMe')
      .mockResolvedValueOnce({ kind: 'error', status: 503, message: 'down' })
      .mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
    render(realApp())

    await userEvent.click(await screen.findByRole('button', { name: /try again/i }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument()
  })

  it('an invite deep-link does not force Signup during an outage', async () => {
    // Otherwise the tester burns their one-time invite against a backend that
    // cannot redeem it, and the token is gone from the URL either way.
    window.history.replaceState({}, '', '/#invite=tok-abc')
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 503, message: 'down' })
    render(realApp())

    expect(await screen.findByRole('button', { name: /try again/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /create account/i })).not.toBeInTheDocument()
  })
})
