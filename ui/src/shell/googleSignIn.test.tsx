/**
 * Sign in with Google, the UI half (lvs7 pr-b).
 *
 * The page never talks to Google: the sign-in control is a link to our own
 * server, and the invite and link controls are native form POSTs to it. These
 * tests pin where the control appears, what it carries, and what it must never
 * carry (the invite in a URL, the password anywhere but the form body).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import * as api from '../api'
import { ThemeProvider } from '../ds/theme'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { GoogleSignInButton } from './GoogleSignInButton'
import { Login } from './Login'
import { Signup } from './Signup'
import { ProfilePage } from './ProfilePage'
import { clearGoogleOutcome, setGoogleOutcome } from './googleOutcome'
import { AppRoot } from '../AppRoot'

function userState(): CurrentUserContextValue {
  return {
    user: {
      id: 'ada@example.com',
      displayName: 'Ada',
      initials: 'A',
      role: 'player',
      signOut: vi.fn(),
      editProfile: vi.fn(),
    },
    authStatus: 'authenticated',
    retryAuthCheck: vi.fn(),
    signIn: vi.fn(),
    setDisplayName: vi.fn(),
    setAvatarTone: vi.fn(),
  }
}

function navState(): AppNavState {
  return {
    screen: 'profile',
    mode: 'sage',
    conversationId: null,
    enterWorkspace: vi.fn(),
    setMode: vi.fn(),
    setConversationId: vi.fn(),
    backToLanding: vi.fn(),
    openProfile: vi.fn(),
    backToWorkspace: vi.fn(),
  }
}

function shell(children: ReactNode) {
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={navState()}>
        <CurrentUserContext.Provider value={userState()}>{children}</CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>
  )
}

function googleOn(on: boolean): void {
  vi.spyOn(api, 'googleAvailable').mockResolvedValue(on)
}

beforeEach(() => {
  vi.spyOn(window, 'open').mockImplementation(() => null)
})

afterEach(() => {
  vi.restoreAllMocks()
  clearGoogleOutcome()
  window.history.replaceState({}, '', '/')
})

describe('GoogleSignInButton', () => {
  it('sign-in is a plain link to our own server, with the guideline text', () => {
    render(<GoogleSignInButton kind="signin" />)
    const link = screen.getByRole('link', { name: 'Sign in with Google' })
    expect(link).toHaveAttribute('href', '/auth/google/start')
    expect(link.getAttribute('href')).not.toMatch(/^https?:/)
  })

  it('sign-up and link are submit buttons with their own guideline text', () => {
    const { unmount } = render(<GoogleSignInButton kind="signup" />)
    expect(screen.getByRole('button', { name: 'Sign up with Google' })).toHaveAttribute('type', 'submit')
    unmount()
    render(<GoogleSignInButton kind="link" />)
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toHaveAttribute('type', 'submit')
  })

  it('the logo is decorative: hidden from assistive tech, so the name is the text alone', () => {
    const { container } = render(<GoogleSignInButton kind="signin" />)
    const svg = container.querySelector('svg')
    expect(svg).not.toBeNull()
    expect(svg).toHaveAttribute('aria-hidden', 'true')
    expect(svg).toHaveAttribute('focusable', 'false')
    expect(screen.getByRole('link')).toHaveAccessibleName('Sign in with Google')
  })

  it('ships the standard four-colour G, unmodified', () => {
    const { container } = render(<GoogleSignInButton kind="signin" />)
    const fills = Array.from(container.querySelectorAll('svg path')).map((p) => p.getAttribute('fill'))
    expect(fills).toEqual(['#EA4335', '#4285F4', '#FBBC05', '#34A853'])
  })

  it('loads no script, opens no window, and never leaves the origin', () => {
    const { container } = render(<GoogleSignInButton kind="signin" />)
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('iframe')).toBeNull()
    expect(window.open).not.toHaveBeenCalled()
    expect(document.querySelector('script[src*="google"]')).toBeNull()
  })
})

describe('Login', () => {
  it('shows no Google control when the feature is off (404) or the check fails', async () => {
    googleOn(false)
    render(shell(<Login />))
    await waitFor(() => expect(api.googleAvailable).toHaveBeenCalled())
    expect(screen.queryByText(/google/i)).toBeNull()
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
  })

  it('shows the sign-in link above the email form when the feature is on, and keeps the form', async () => {
    googleOn(true)
    render(shell(<Login />))
    const link = await screen.findByRole('link', { name: 'Sign in with Google' })
    expect(link).toHaveAttribute('href', '/auth/google/start')
    const emailField = screen.getByLabelText('Email')
    expect(link.compareDocumentPosition(emailField) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    // Google's own control is a link, not a second "Sign in" button.
    expect(screen.getAllByRole('button', { name: /sign in/i })).toHaveLength(1)
  })

  it('says the outcome the callback redirected with, as an alert, and never the raw code', () => {
    googleOn(false)
    setGoogleOutcome({ search: '?google=no_account', pathname: '/' })
    render(shell(<Login />))
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent("We couldn't find an account for that Google sign-in.")
    expect(alert).not.toHaveTextContent('no_account')
  })

  it('renders a hostile value as the failed text, as text', () => {
    googleOn(false)
    setGoogleOutcome({ search: `?google=${encodeURIComponent('<img src=x onerror=alert(1)>')}`, pathname: '/' })
    const { container } = render(shell(<Login />))
    expect(screen.getByRole('alert')).toHaveTextContent("We couldn't sign you in with Google. Please try again.")
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('Signup (invite in hand)', () => {
  const INVITE = 'inv-AbC_123-xyz'

  it('shows no Google control when the feature is off', async () => {
    googleOn(false)
    render(shell(<Signup invite={INVITE} onUseLogin={vi.fn()} />))
    await waitFor(() => expect(api.googleAvailable).toHaveBeenCalled())
    expect(screen.queryByText(/google/i)).toBeNull()
  })

  it('posts the invite in a form body, never in a URL, and keeps the email form', async () => {
    googleOn(true)
    const { container } = render(shell(<Signup invite={INVITE} onUseLogin={vi.fn()} />))
    const button = await screen.findByRole('button', { name: 'Sign up with Google' })
    const form = button.closest('form')
    expect(form).not.toBeNull()
    expect(form).toHaveAttribute('method', 'post')
    expect(form).toHaveAttribute('action', '/auth/google/start')
    const fields = Array.from(form!.querySelectorAll('input')).map((i) => [i.name, i.type, i.value])
    expect(fields).toEqual([
      ['intent', 'hidden', 'invite'],
      ['invite', 'hidden', INVITE],
    ])
    // The token appears in exactly one place: that hidden input.
    expect(container.innerHTML.split(INVITE)).toHaveLength(2)
    for (const el of Array.from(container.querySelectorAll('[href], [action], [src]'))) {
      for (const attr of ['href', 'action', 'src']) {
        expect(el.getAttribute(attr) ?? '').not.toContain(INVITE)
      }
    }
    expect(window.location.href).not.toContain(INVITE)
    // The password sign-up form is still there, and is a different form.
    expect(screen.getByRole('button', { name: 'Create account' }).closest('form')).not.toBe(form)
  })

  it('says why an invite did not work', () => {
    googleOn(false)
    setGoogleOutcome({ search: '?google=invite_retry', pathname: '/' })
    render(shell(<Signup invite={INVITE} onUseLogin={vi.fn()} />))
    expect(screen.getByRole('alert')).toHaveTextContent('Open your invite link again')
  })
})

describe('ProfilePage', () => {
  it('has no Google section when the feature is off', async () => {
    googleOn(false)
    const link = vi.spyOn(api, 'getGoogleLink')
    render(shell(<ProfilePage />))
    await waitFor(() => expect(api.googleAvailable).toHaveBeenCalled())
    expect(screen.queryByRole('region', { name: 'Google account' })).toBeNull()
    expect(screen.queryByText(/google/i)).toBeNull()
    expect(link).not.toHaveBeenCalled()
  })

  it('has no Google section when the link status cannot be read', async () => {
    googleOn(true)
    vi.spyOn(api, 'getGoogleLink').mockResolvedValue(null)
    render(shell(<ProfilePage />))
    await waitFor(() => expect(api.getGoogleLink).toHaveBeenCalled())
    expect(screen.queryByRole('region', { name: 'Google account' })).toBeNull()
  })

  it('linked: says which address, as text, and offers no unlink', async () => {
    googleOn(true)
    vi.spyOn(api, 'getGoogleLink').mockResolvedValue({
      linked: true, email: '<b>ada</b>@gmail.example', hasPassword: true,
    })
    const { container } = render(shell(<ProfilePage />))
    const section = await screen.findByRole('region', { name: 'Google account' })
    expect(section).toHaveTextContent('Linked to <b>ada</b>@gmail.example')
    expect(section.querySelector('b')).toBeNull()
    expect(container.querySelector('form[action="/auth/google/start"]')).toBeNull()
    expect(within(section).queryByRole('button')).toBeNull()
    expect(screen.queryByRole('button', { name: /unlink|disconnect|remove/i })).toBeNull()
  })

  it('a Google-only account is told so, with nothing to submit', async () => {
    googleOn(true)
    vi.spyOn(api, 'getGoogleLink').mockResolvedValue({
      linked: true, email: 'ada@gmail.example', hasPassword: false,
    })
    render(shell(<ProfilePage />))
    const section = await screen.findByRole('region', { name: 'Google account' })
    expect(section).toHaveTextContent('You sign in with Google.')
    expect(within(section).queryByLabelText(/password/i)).toBeNull()
  })

  it('not linked: a POST form with intent=link and a labelled current-password field', async () => {
    googleOn(true)
    vi.spyOn(api, 'getGoogleLink').mockResolvedValue({ linked: false, email: null, hasPassword: true })
    render(shell(<ProfilePage />))
    const section = await screen.findByRole('region', { name: 'Google account' })
    const form = section.querySelector('form')
    expect(form).not.toBeNull()
    expect(form).toHaveAttribute('method', 'post')
    expect(form).toHaveAttribute('action', '/auth/google/start')
    const intent = form!.querySelector<HTMLInputElement>('input[name="intent"]')
    expect(intent).toHaveAttribute('type', 'hidden')
    expect(intent).toHaveValue('link')

    const password = within(section).getByLabelText('Current password')
    expect(password).toHaveAttribute('type', 'password')
    expect(password).toHaveAttribute('name', 'password')
    expect(password).toHaveAttribute('autocomplete', 'current-password')
    expect(password).toBeRequired()
    expect(form!.contains(password)).toBe(true)

    const submit = within(section).getByRole('button', { name: 'Continue with Google' })
    expect(submit).toHaveAttribute('type', 'submit')
    expect(form!.contains(submit)).toBe(true)
    // Only the two fields the server accepts.
    expect(Array.from(form!.querySelectorAll('input')).map((i) => i.name).sort()).toEqual(['intent', 'password'])
  })

  it('the outcome of a link attempt is announced on this page, and only if it landed here', async () => {
    googleOn(true)
    vi.spyOn(api, 'getGoogleLink').mockResolvedValue({ linked: true, email: 'ada@gmail.example', hasPassword: true })
    setGoogleOutcome({ search: '?google=linked', pathname: '/profile' })
    const { unmount } = render(shell(<ProfilePage />))
    expect(screen.getByRole('status')).toHaveTextContent('Google account linked. You can now sign in with Google.')
    expect(screen.queryByRole('alert')).toBeNull()
    unmount()

    clearGoogleOutcome()
    setGoogleOutcome({ search: '?google=reauth_failed', pathname: '/profile' })
    const second = render(shell(<ProfilePage />))
    expect(screen.getByRole('alert')).toHaveTextContent("That password isn't right, so Google wasn't linked.")
    second.unmount()

    clearGoogleOutcome()
    setGoogleOutcome({ search: '?google=failed', pathname: '/' })
    render(shell(<ProfilePage />))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

// The shipped provider tower, cold-loaded the way the server's redirect lands.
describe('the callback redirect, through AppRoot', () => {
  it('signed out: the sign-in screen says why, and the address bar is clean', async () => {
    googleOn(false)
    window.history.replaceState({}, '', '/?google=no_account')
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    render(<AppRoot />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("We couldn't find an account for that Google sign-in.")
    expect(window.location.search).toBe('')
    expect(window.location.pathname).toBe('/')
  })

  it('a successful link lands on Profile with a polite status', async () => {
    googleOn(true)
    vi.spyOn(api, 'getGoogleLink').mockResolvedValue({ linked: true, email: 'ada@gmail.example', hasPassword: true })
    window.history.replaceState({}, '', '/profile?google=linked')
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'player' } })
    render(<AppRoot />)
    expect(await screen.findByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Google account linked.')
    expect(await screen.findByText('Linked to ada@gmail.example')).toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect(window.location.pathname).toBe('/profile')
  })
})
