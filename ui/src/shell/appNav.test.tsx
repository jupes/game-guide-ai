import { describe, it, expect, vi } from 'vitest'
import { renderHook, act, render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavProvider, AppNavContext, useAppNav } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext, CurrentUserProvider } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ThemeProvider } from '../ds/theme'
import { Landing } from './Landing'
import { CampaignProvider } from './campaignContext'
import App from '../App'
import * as api from '../api'

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
    authStatus: 'authenticated',
    retryAuthCheck: vi.fn(),
    signIn: vi.fn(),
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
  it('renders the ProfilePage when screen is profile', async () => {
    // App holds a loading state until the session check resolves (x5bz.2).
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'tester@example.com', role: 'player' },
    })
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={makeNavState({ screen: 'profile' })}>
          <CurrentUserProvider>
            <App />
          </CurrentUserProvider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    expect(await screen.findByRole('heading', { name: 'Profile' })).toBeInTheDocument()
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

  // 30c ID-1, ID-22 (L-1): the CTA opens the tavern for every signed-in account,
  // a player's included (PR-2 built its tavern); the workspace is the chips' way in.
  it('L-1 the CTA of a dm and that of a player both open the tavern, never the workspace', async () => {
    const stub = vi.fn(async () => new Response('{}', { status: 200 })) as unknown as typeof fetch
    const mount = (role: 'dm' | 'player') => {
      const enterWorkspace = vi.fn()
      const openTavern = vi.fn()
      const state: AppNavState = { ...makeNavState({ screen: 'landing' }), enterWorkspace, openTavern }
      render(
        <AppNavContext.Provider value={state}>
          <CurrentUserContext.Provider value={makeUserState(role)}>
            <CampaignProvider fetchImpl={stub}>
              <Landing />
            </CampaignProvider>
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>,
      )
      return { enterWorkspace, openTavern }
    }
    const dm = mount('dm')
    await userEvent.click(screen.getByRole('button', { name: /Enter the Tavern/i }))
    expect(dm.openTavern).toHaveBeenCalledTimes(1)
    expect(dm.enterWorkspace).not.toHaveBeenCalled()
    cleanup()

    const player = mount('player')
    await userEvent.click(screen.getByRole('button', { name: /Enter the Tavern/i }))
    expect(player.openTavern).toHaveBeenCalledTimes(1)
    expect(player.enterWorkspace).not.toHaveBeenCalled()
    // Landing's own reads and writes: none for either account.
    expect(stub).not.toHaveBeenCalled()
  })

  it('L-3 the mode chips still enter the workspace in their mode, for a dm too', async () => {
    const enterWorkspace = vi.fn()
    const openTavern = vi.fn()
    const state: AppNavState = { ...makeNavState({ screen: 'landing' }), enterWorkspace, openTavern }
    render(
      <AppNavContext.Provider value={state}>
        <CurrentUserContext.Provider value={makeUserState('dm')}>
          <CampaignProvider fetchImpl={vi.fn() as unknown as typeof fetch}>
            <Landing />
          </CampaignProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Sage' }))
    await userEvent.click(screen.getByRole('button', { name: 'GM' }))
    expect(enterWorkspace.mock.calls).toEqual([['sage'], ['gm']])
    expect(openTavern).not.toHaveBeenCalled()
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
