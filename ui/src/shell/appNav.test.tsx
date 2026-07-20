import { describe, it, expect, vi } from 'vitest'
import { renderHook, act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavProvider, AppNavContext, useAppNav } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext, CurrentUserProvider } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ThemeProvider } from '../ds/theme'
import { Landing } from './Landing'
import App from '../App'

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

// ── CP-F3.1 — AppNav context behaviors (#12) ──────────────────────────────────

describe('AppNav context (#12)', () => {
  it('starts on the landing screen', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    expect(result.current.screen).toBe('landing')
  })

  it('enterWorkspace() sets screen to workspace with default mode sage', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.enterWorkspace()
    })
    expect(result.current.screen).toBe('workspace')
    expect(result.current.mode).toBe('sage')
  })

  it('enterWorkspace("spell") sets screen to workspace with mode spell', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.enterWorkspace('spell')
    })
    expect(result.current.screen).toBe('workspace')
    expect(result.current.mode).toBe('spell')
  })

  it('setMode("spell") updates mode to spell', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.setMode('spell')
    })
    expect(result.current.mode).toBe('spell')
  })

  it('backToLanding() sets screen to landing', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.enterWorkspace()
    })
    act(() => {
      result.current.backToLanding()
    })
    expect(result.current.screen).toBe('landing')
  })

  // ── swe1.7 — profile screen ────────────────────────────────────────────────

  it('openProfile() sets screen to profile', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => result.current.openProfile())
    expect(result.current.screen).toBe('profile')
  })

  it('backToWorkspace() returns to the workspace and preserves the mode', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => result.current.enterWorkspace('gm'))
    act(() => result.current.openProfile())
    act(() => result.current.backToWorkspace())
    expect(result.current.screen).toBe('workspace')
    expect(result.current.mode).toBe('gm') // unlike enterWorkspace, mode is untouched
  })
})

describe('App profile screen (swe1.7)', () => {
  it('renders the ProfilePage when screen is profile', () => {
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={makeNavState({ screen: 'profile' })}>
          <CurrentUserProvider>
            <App />
          </CurrentUserProvider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    expect(screen.getByRole('heading', { name: 'Profile' })).toBeInTheDocument()
  })
})

// ── CP-F3.2 — Landing component ───────────────────────────────────────────────

describe('Landing component', () => {
  it('renders brand text and CTA button', () => {
    const enterWorkspace = vi.fn()
    const mockState: AppNavState = {
      screen: 'landing',
      mode: 'sage',
      conversationId: null,
      enterWorkspace,
      setMode: vi.fn(),
      setConversationId: vi.fn(),
      backToLanding: vi.fn(),
      openProfile: vi.fn(),
      backToWorkspace: vi.fn(),
    }

    render(
      <AppNavContext.Provider value={mockState}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <Landing />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )

    expect(screen.getByText('Aetheril')).toBeInTheDocument()
    expect(screen.getByText(/Grounded answers from the rulebooks/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Enter the Tavern/i })).toBeInTheDocument()
  })

  it('clicking CTA calls enterWorkspace', async () => {
    const enterWorkspace = vi.fn()
    const mockState: AppNavState = {
      screen: 'landing',
      mode: 'sage',
      conversationId: null,
      enterWorkspace,
      setMode: vi.fn(),
      setConversationId: vi.fn(),
      backToLanding: vi.fn(),
      openProfile: vi.fn(),
      backToWorkspace: vi.fn(),
    }

    render(
      <AppNavContext.Provider value={mockState}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <Landing />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )

    await userEvent.click(screen.getByRole('button', { name: /Enter the Tavern/i }))
    expect(enterWorkspace).toHaveBeenCalledTimes(1)
  })

  // channel-chats CP-D — the GM entry chip is DM-only
  it('shows the GM entry chip to a dm and hides it from a player', () => {
    const mockState: AppNavState = {
      screen: 'landing',
      mode: 'sage',
      conversationId: null,
      enterWorkspace: vi.fn(),
      setMode: vi.fn(),
      setConversationId: vi.fn(),
      backToLanding: vi.fn(),
      openProfile: vi.fn(),
      backToWorkspace: vi.fn(),
    }

    const { unmount } = render(
      <AppNavContext.Provider value={mockState}>
        <CurrentUserContext.Provider value={makeUserState('dm')}>
          <Landing />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )
    expect(screen.getByText('GM')).toBeInTheDocument()
    unmount()

    render(
      <AppNavContext.Provider value={mockState}>
        <CurrentUserContext.Provider value={makeUserState('player')}>
          <Landing />
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )
    expect(screen.queryByText('GM')).not.toBeInTheDocument()
    expect(screen.getByText('Sage')).toBeInTheDocument()
  })
})
