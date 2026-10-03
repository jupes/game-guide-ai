/**
 * LoginRevealNotice.test.tsx -- REVEAL-16 (agent-forge-harness-1kg.7.3 PR-2): a 401 during a live
 * reveal says `The table can still see <title>` on the screen the GM lands on, and a successful
 * sign-in clears it. Memory only; the title is rendered and nothing more.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { ThemeProvider } from '../ds/theme'
import { AppNavProvider } from './AppNav'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { CurrentUserContext, type CurrentUserContextValue } from './currentUser'
import { Login } from './Login'
import { revealSignOutNotice } from './revealSignOut'

function userState(overrides: Partial<CurrentUserContextValue> = {}): CurrentUserContextValue {
  return {
    user: { id: 'guest', displayName: 'Adventurer', initials: 'AV', role: 'player', signOut: vi.fn(), editProfile: vi.fn() },
    authStatus: 'unauthenticated',
    retryAuthCheck: vi.fn(),
    signIn: vi.fn(),
    setDisplayName: vi.fn(),
    setAvatarTone: vi.fn(),
    ...overrides,
  }
}

const mount = (user: CurrentUserContextValue): void => {
  render(
    <ThemeProvider>
      <AppNavProvider>
        <CurrentUserContext.Provider value={user}>
          <ConversationStoreProvider store={new MemoryConversationStore()}>
            <Login />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavProvider>
    </ThemeProvider>,
  )
}

beforeEach(() => revealSignOutNotice.clear())
afterEach(() => {
  revealSignOutNotice.clear()
  vi.restoreAllMocks()
  window.history.replaceState({}, '', '/')
})

describe('the sign-in screen after a 401 during a live reveal', () => {
  it('says nothing when nothing was live', () => {
    mount(userState())
    expect(screen.queryByText(/The table can still see/)).toBeNull()
  })

  it('says what the table can still see, as an alert, with the title as text', () => {
    revealSignOutNotice.set(['Ondrey'])
    mount(userState())
    expect(screen.getByRole('alert')).toHaveTextContent('The table can still see Ondrey')
  })

  it('names two, and counts past two', () => {
    revealSignOutNotice.set(['Ondrey', 'Mira'])
    mount(userState())
    expect(screen.getByRole('alert')).toHaveTextContent('The table can still see Ondrey and Mira')
    act(() => revealSignOutNotice.set(['A', 'B', 'C', 'D']))
    expect(screen.getByRole('alert')).toHaveTextContent('The table can still see A, B and 2 more')
  })

  it('never reads a title as markup', () => {
    revealSignOutNotice.set(['<img src=x onerror=alert(1)>'])
    mount(userState())
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('<img src=x onerror=alert(1)>')
  })

  it('clears once the GM has signed in again', async () => {
    revealSignOutNotice.set(['Ondrey'])
    const signIn = vi.fn()
    vi.spyOn(api, 'login').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
    mount(userState({ signIn }))
    await userEvent.type(screen.getByLabelText(/email/i), 'ada@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'password123')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    await waitFor(() => expect(signIn).toHaveBeenCalled())
    expect(revealSignOutNotice.text).toBeNull()
  })

  it('stays after a failed sign-in: the table can still see it', async () => {
    revealSignOutNotice.set(['Ondrey'])
    vi.spyOn(api, 'login').mockResolvedValue({ kind: 'error', message: 'Wrong email or password.' })
    mount(userState())
    await userEvent.type(screen.getByLabelText(/email/i), 'ada@example.com')
    await userEvent.type(screen.getByLabelText(/password/i), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('Wrong email or password.')).toBeInTheDocument()
    expect(screen.getByText('The table can still see Ondrey')).toBeInTheDocument()
  })
})
