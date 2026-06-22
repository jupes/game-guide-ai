import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ThemeProvider } from '../ds/theme'
import { LeftNav } from './LeftNav'
import { TopBar } from './TopBar'

// ── CP-F3.3 — LeftNav behaviors (#13) ────────────────────────────────────────

function makeNavState(overrides: Partial<AppNavState> = {}): AppNavState {
  return {
    screen: 'workspace',
    mode: 'sage',
    conversationId: null,
    enterWorkspace: vi.fn(),
    setMode: vi.fn(),
    setConversationId: vi.fn(),
    backToLanding: vi.fn(),
    ...overrides,
  }
}

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

describe('LeftNav (#13)', () => {
  it('renders all 4 mode labels', () => {
    const navState = makeNavState()
    const userState = makeUserState()
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={navState}>
          <CurrentUserContext.Provider value={userState}>
            <LeftNav />
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    expect(screen.getByText('Sage')).toBeInTheDocument()
    expect(screen.getByText('Spell')).toBeInTheDocument()
    expect(screen.getByText('Rules')).toBeInTheDocument()
    expect(screen.getByText('GM')).toBeInTheDocument()
  })

  it('clicking the Spell chip calls setMode("spell")', async () => {
    const setMode = vi.fn()
    const navState = makeNavState({ setMode })
    const userState = makeUserState()
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={navState}>
          <CurrentUserContext.Provider value={userState}>
            <LeftNav />
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    await userEvent.click(screen.getByText('Spell'))
    expect(setMode).toHaveBeenCalledWith('spell')
  })

  it('the Sage chip is marked selected when mode is sage', () => {
    const navState = makeNavState({ mode: 'sage' })
    const userState = makeUserState()
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={navState}>
          <CurrentUserContext.Provider value={userState}>
            <LeftNav />
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    // The selected chip gets the chip--selected class
    const sageChip = screen.getByText('Sage').closest('.chip')
    expect(sageChip).toHaveClass('chip--selected')
  })
})

// ── TopBar / theme toggle ──────────────────────────────────────────────────────

describe('TopBar theme toggle', () => {
  beforeEach(() => {
    // Ensure clean state
    document.documentElement.removeAttribute('data-theme')
  })

  afterEach(() => {
    document.documentElement.removeAttribute('data-theme')
  })

  it('clicking the theme Switch changes document.documentElement data-theme', async () => {
    render(
      <ThemeProvider initialTheme="light">
        <TopBar />
      </ThemeProvider>,
    )
    const toggle = screen.getByRole('switch', { name: /dark theme/i })
    expect(document.documentElement.getAttribute('data-theme')).toBeNull()
    await userEvent.click(toggle)
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })
})
