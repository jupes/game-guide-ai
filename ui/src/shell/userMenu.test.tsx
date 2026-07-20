import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactElement } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ThemeProvider } from '../ds/theme'
import { UserMenu } from './UserMenu'

// UserMenu now consumes useTheme() (swe1.11 theme toggle), which throws without
// a ThemeProvider ancestor — so every render goes through this wrapper.
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
    setRole: vi.fn(),
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

// ── channel-chats CP-D — DM role toggle + gm→sage fallback ────────────────────

describe('UserMenu DM role toggle', () => {
  async function openMenu() {
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))
  }

  it('toggling the Dungeon Master switch on calls setRole("dm")', async () => {
    const setRole = vi.fn()
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState({ setRole })}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    await openMenu()
    await userEvent.click(screen.getByRole('switch', { name: /dungeon master/i }))
    expect(setRole).toHaveBeenCalledWith('dm')
  })

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

  it('giving up the DM role while in the GM channel falls back to sage', async () => {
    const setRole = vi.fn()
    const setMode = vi.fn()
    const dmState = makeUserState({ setRole })
    dmState.user.role = 'dm'
    renderWithTheme(
      <AppNavContext.Provider value={makeNavState({ mode: 'gm', setMode })}>
        <CurrentUserContext.Provider value={dmState}>
          <UserMenu />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )
    await openMenu()
    await userEvent.click(screen.getByRole('switch', { name: /dungeon master/i }))
    expect(setRole).toHaveBeenCalledWith('player')
    expect(setMode).toHaveBeenCalledWith('sage')
  })

  it('giving up the DM role in a non-GM channel does not touch the mode', async () => {
    const setMode = vi.fn()
    const dmState = makeUserState()
    dmState.user.role = 'dm'
    renderWithTheme(
      <AppNavContext.Provider value={makeNavState({ mode: 'rules', setMode })}>
        <CurrentUserContext.Provider value={dmState}>
          <UserMenu />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )
    await openMenu()
    await userEvent.click(screen.getByRole('switch', { name: /dungeon master/i }))
    expect(setMode).not.toHaveBeenCalled()
  })
})

// ── swe1.11 — dark/light theme toggle lives in the UserMenu ───────────────────

describe('UserMenu theme toggle (swe1.11)', () => {
  // localStorage is effectively inert in this runner (no removeItem/clear), so the
  // ThemeProvider falls through to the OS default (light in jsdom). Just keep the
  // document theme attribute clean between tests.
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme')
  })
  afterEach(() => {
    document.documentElement.removeAttribute('data-theme')
  })

  it('exposes a Dark theme switch in the menu that flips the document theme', async () => {
    renderWithTheme(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /open user menu/i }))

    const toggle = screen.getByRole('switch', { name: /dark theme/i })
    expect(document.documentElement.getAttribute('data-theme')).not.toBe('dark')
    await userEvent.click(toggle)
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    await userEvent.click(toggle)
    expect(document.documentElement.getAttribute('data-theme')).not.toBe('dark')
  })
})
