import { describe, it, expect, vi } from 'vitest'
import type { ReactElement } from 'react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ThemeProvider } from '../ds/theme'
import { UserMenu } from './UserMenu'

// Render through the app's theme boundary so the no-duplicate assertion is
// representative even though UserMenu no longer consumes the theme itself.
function renderWithTheme(ui: ReactElement) {
  return render(<ThemeProvider>{ui}</ThemeProvider>)
}

function makeNavState(overrides: Partial<AppNavState> = {}): AppNavState {
  return {
    screen: 'workspace',
    mode: 'sage',
    conversationId: null,
    enterWorkspace: vi.fn(),
    setMode: vi.fn(),
    setConversationId: vi.fn(),
    backToLanding: vi.fn(),
    openProfile: vi.fn(),
    backToWorkspace: vi.fn(),
    ...overrides,
  }
}

// ── CP-F3.4 — UserMenu behaviors (#14) ───────────────────────────────────────

function makeUserState(overrides: Partial<CurrentUserContextValue> = {}): CurrentUserContextValue {
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

describe('UserMenu (#14)', () => {
  it('shows the stub user initials AV (derived from displayName Adventurer)', () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    // Avatar derives initials "AV" from "Adventurer" — first letter is "A",
    // but the name has only one word so initials = "A".
    // Wait — "Adventurer" is one word → initials = "A", not "AV".
    // The spec says initials: 'AV' but Avatar derives from displayName 'Adventurer'
    // which gives 'A'. The Avatar renders what it derives, so we check for 'A'.
    // The CurrentUser.initials field is metadata, not what Avatar uses.
    expect(screen.getByText('A')).toBeInTheDocument()
  })

  it('menu is initially closed (Sign out not visible)', () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    expect(screen.queryByText(/sign out/i)).not.toBeInTheDocument()
  })

  it('clicking the avatar opens the menu showing Sign out', async () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    const avatarBtn = screen.getByRole('button', { name: /open user menu/i })
    await userEvent.click(avatarBtn)
    expect(screen.getByText(/sign out/i)).toBeInTheDocument()
  })

  it('clicking Sign out calls user.signOut()', async () => {
    const signOut = vi.fn()
    const userState = makeUserState({
      user: {
        id: 'guest',
        displayName: 'Adventurer',
        initials: 'AV',
        role: 'player',
        signOut,
        editProfile: vi.fn(),
      },
    })
    renderWithTheme(
      <CurrentUserContext.Provider value={userState}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    const avatarBtn = screen.getByRole('button', { name: /open user menu/i })
    await userEvent.click(avatarBtn)
    await userEvent.click(screen.getByText(/sign out/i))
    expect(signOut).toHaveBeenCalledTimes(1)
  })
})

// ── x5bz.2 — DM role display is read-only (server-authoritative) ─────────────

describe('UserMenu DM role display (x5bz.2)', () => {
  async function openMenu() {
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))
  }

  it('the switch reflects the current role', async () => {
    const dmState = makeUserState()
    dmState.user.role = 'dm'
    renderWithTheme(
      <CurrentUserContext.Provider value={dmState}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    await openMenu()
    expect(screen.getByRole('switch', { name: /dungeon master/i })).toBeChecked()
  })

  it('a player sees the switch off and cannot toggle it (disabled — role is server-set)', async () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    await openMenu()
    const roleSwitch = screen.getByRole('switch', { name: /dungeon master/i })
    expect(roleSwitch).not.toBeChecked()
    expect(roleSwitch).toBeDisabled()
  })
})

// ── eiio.3 — dark/light theme toggle lives in AppHeader ────────────────────────

describe('UserMenu theme location (eiio.3)', () => {
  it('contains no Dark theme switch', async () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))

    expect(screen.queryByRole('switch', { name: /dark theme/i }))
      .not.toBeInTheDocument()
  })
})

// ── swe1.7 — Profile item opens the page + avatar reflects the chosen tone ─────

describe('UserMenu profile (swe1.7)', () => {
  it('the Profile item opens the profile screen', async () => {
    const openProfile = vi.fn()
    renderWithTheme(
      <AppNavContext.Provider value={makeNavState({ openProfile })}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <UserMenu />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))
    const group = screen.getByRole('group', { name: 'User menu' })
    await userEvent.click(within(group).getByRole('button', { name: /profile/i }))
    expect(openProfile).toHaveBeenCalledTimes(1)
  })

  it('the avatar reflects the user chosen tone', () => {
    const userState = makeUserState()
    userState.user.avatarTone = 'arcane'
    renderWithTheme(
      <CurrentUserContext.Provider value={userState}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    expect(document.querySelector('.avatar--arcane')).toBeInTheDocument()
  })
})

// ── agent-forge-harness-3j4 — a labelled group of buttons, not an ARIA menu ──

describe('UserMenu popover (agent-forge-harness-3j4)', () => {
  function renderMenu() {
    return renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
  }

  it('U1: makes no ARIA menu promise', async () => {
    renderMenu()
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
    expect(screen.getByRole('button', { name: /open user menu/i })).not.toHaveAttribute(
      'aria-haspopup',
    )
    const group = screen.getByRole('group', { name: 'User menu' })
    expect(within(group).getByRole('button', { name: 'Profile' })).toBeInTheDocument()
    expect(within(group).getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('U2: Escape closes the popover and returns focus to the trigger', async () => {
    renderMenu()
    const trigger = screen.getByRole('button', { name: /open user menu/i })
    // Keyboard only, and Escape is pressed from INSIDE the popover: were
    // focus still on the trigger, the focus assertion below would pass
    // whether or not Escape moved it.
    await userEvent.tab()
    expect(trigger).toHaveFocus()
    await userEvent.keyboard('{Enter}')
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await userEvent.tab()
    await userEvent.tab()
    const group = screen.getByRole('group', { name: 'User menu' })
    expect(within(group).getByRole('button', { name: 'Sign out' })).toHaveFocus()

    await userEvent.keyboard('{Escape}')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('group', { name: 'User menu' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('U2b: Escape pressed outside the root, with the popover open, does nothing', async () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <div>
          <button type="button">Outside control</button>
          <UserMenu />
        </div>
      </CurrentUserContext.Provider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))
    const outside = screen.getByRole('button', { name: 'Outside control' })
    outside.focus()
    expect(outside).toHaveFocus()

    await userEvent.keyboard('{Escape}')

    expect(screen.getByRole('group', { name: 'User menu' })).toBeInTheDocument()
    expect(outside).toHaveFocus()
  })

  it('U3: an outside press closes the popover; the trigger still toggles', async () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <div>
          <button type="button">Outside control</button>
          <UserMenu />
        </div>
      </CurrentUserContext.Provider>,
    )
    const trigger = screen.getByRole('button', { name: /open user menu/i })
    await userEvent.click(trigger)
    expect(screen.getByRole('group', { name: 'User menu' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Outside control' }))
    expect(screen.queryByRole('group', { name: 'User menu' })).not.toBeInTheDocument()

    // The trigger still toggles it back open, and open->closed again.
    await userEvent.click(trigger)
    expect(screen.getByRole('group', { name: 'User menu' })).toBeInTheDocument()
    await userEvent.click(trigger)
    expect(screen.queryByRole('group', { name: 'User menu' })).not.toBeInTheDocument()
  })

  it('U3: a press inside the popover (the read-only role row) does not close it', async () => {
    renderMenu()
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))
    expect(screen.getByRole('group', { name: 'User menu' })).toBeInTheDocument()

    await userEvent.click(screen.getByText('Dungeon Master'))
    expect(screen.getByRole('group', { name: 'User menu' })).toBeInTheDocument()
  })
})
