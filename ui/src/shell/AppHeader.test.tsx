/**
 * AppHeader (swe1.4) — persistent channel switcher band.
 *
 * Behaviors: role-gated channel list, switching via click + keyboard, active
 * channel marked with its accent, and the persistent theme control.
 */

import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { AppHeader } from './AppHeader'

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

function makeUserState(role: 'dm' | 'player' = 'player'): CurrentUserContextValue {
  return {
    user: {
      id: 'guest',
      displayName: 'Adventurer',
      initials: 'AV',
      role,
      signOut: vi.fn(),
      editProfile: vi.fn(),
    },
    setRole: vi.fn(),
    setDisplayName: vi.fn(),
    setAvatarTone: vi.fn(),
  }
}

function renderHeader(nav: AppNavState, user = makeUserState()) {
  return render(
    <ThemeProvider initialTheme="light">
      <AppNavContext.Provider value={nav}>
        <CurrentUserContext.Provider value={user}>
          <AppHeader />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>,
  )
}

describe('AppHeader (swe1.4)', () => {
  afterEach(() => {
    document.documentElement.removeAttribute('data-theme')
  })

  it('renders all 4 channels for a dm', () => {
    renderHeader(makeNavState(), makeUserState('dm'))
    for (const name of ['Sage', 'Spell', 'Rules', 'GM']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    }
  })

  it('hides the GM channel from a player', () => {
    renderHeader(makeNavState(), makeUserState('player'))
    expect(screen.getByRole('button', { name: 'Sage' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'GM' })).not.toBeInTheDocument()
  })

  it('clicking a channel calls setMode with its id', async () => {
    const setMode = vi.fn()
    renderHeader(makeNavState({ setMode }))
    await userEvent.click(screen.getByRole('button', { name: 'Spell' }))
    expect(setMode).toHaveBeenCalledWith('spell')
  })

  it('activates a channel via keyboard (Enter)', async () => {
    const setMode = vi.fn()
    renderHeader(makeNavState({ setMode }))
    screen.getByRole('button', { name: 'Rules' }).focus()
    await userEvent.keyboard('{Enter}')
    expect(setMode).toHaveBeenCalledWith('rules')
  })

  it('marks the active channel selected with its accent class', () => {
    renderHeader(makeNavState({ mode: 'spell' }), makeUserState('dm'))
    const spell = screen.getByRole('button', { name: 'Spell' })
    expect(spell).toHaveClass('chip--selected')
    expect(spell).toHaveClass('mode-accent--arcane')
    // A non-active channel carries its accent class but is not selected.
    const sage = screen.getByRole('button', { name: 'Sage' })
    expect(sage).toHaveClass('mode-accent--verdigris')
    expect(sage).not.toHaveClass('chip--selected')
  })

  it('places a keyboard-operable Dark theme switch in the right-hand slot', async () => {
    const { container } = renderHeader(makeNavState())
    const toggle = screen.getByRole('switch', { name: 'Dark theme' })

    expect(container.querySelector('.app-header__theme')).toContainElement(toggle)
    expect(screen.getByText('Dark theme')).toBeInTheDocument()
    expect(toggle).not.toBeChecked()

    toggle.focus()
    await userEvent.keyboard(' ')

    expect(toggle).toBeChecked()
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  })
})
