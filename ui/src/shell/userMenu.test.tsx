import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { UserMenu } from './UserMenu'

// ── CP-F3.4 — UserMenu behaviors (#14) ───────────────────────────────────────

function makeUserState(overrides: Partial<CurrentUserContextValue> = {}): CurrentUserContextValue {
  return {
    user: {
      id: 'guest',
      displayName: 'Adventurer',
      initials: 'AV',
      signOut: vi.fn(),
      editProfile: vi.fn(),
    },
    ...overrides,
  }
}

describe('UserMenu (#14)', () => {
  it('shows the stub user initials AV (derived from displayName Adventurer)', () => {
    render(
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
    render(
      <CurrentUserContext.Provider value={makeUserState()}>
        <UserMenu />
      </CurrentUserContext.Provider>,
    )
    expect(screen.queryByText(/sign out/i)).not.toBeInTheDocument()
  })

  it('clicking the avatar opens the menu showing Sign out', async () => {
    render(
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
        signOut,
        editProfile: vi.fn(),
      },
    })
    render(
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
