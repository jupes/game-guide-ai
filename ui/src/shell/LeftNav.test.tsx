import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
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

function renderLeftNav(navState: AppNavState, store = new MemoryConversationStore()) {
  return render(
    <ThemeProvider>
      <AppNavContext.Provider value={navState}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <ConversationStoreProvider store={store}>
            <LeftNav />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>,
  )
}

describe('LeftNav (#13)', () => {
  it('renders all 4 mode labels', () => {
    const navState = makeNavState()
    const userState = makeUserState()
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={navState}>
          <CurrentUserContext.Provider value={userState}>
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <LeftNav />
            </ConversationStoreProvider>
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
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <LeftNav />
            </ConversationStoreProvider>
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
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <LeftNav />
            </ConversationStoreProvider>
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    // The selected chip gets the chip--selected class
    const sageChip = screen.getByText('Sage').closest('.chip')
    expect(sageChip).toHaveClass('chip--selected')
  })
})

// ── CP-F5.4 — LeftNav conversation list (#22) ────────────────────────────────

describe('LeftNav conversation list (#22)', () => {
  it('lists conversations for the active mode', () => {
    const store = new MemoryConversationStore()
    store.create('sage', 'Dragon Lore')
    store.create('sage', 'Basilisk Info')
    store.create('spell', 'Fireball details') // different mode — should not appear

    renderLeftNav(makeNavState({ mode: 'sage' }), store)

    expect(screen.getByText('Dragon Lore')).toBeInTheDocument()
    expect(screen.getByText('Basilisk Info')).toBeInTheDocument()
    expect(screen.queryByText('Fireball details')).not.toBeInTheDocument()
  })

  it('clicking a conversation calls setConversationId with its id', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', 'Dragon Lore')
    const setConversationId = vi.fn()

    renderLeftNav(makeNavState({ setConversationId }), store)

    await userEvent.click(screen.getByText('Dragon Lore'))
    expect(setConversationId).toHaveBeenCalledWith(conv.id)
  })

  it('clicking "New conversation" creates a conversation and calls setConversationId', async () => {
    const store = new MemoryConversationStore()
    const setConversationId = vi.fn()

    renderLeftNav(makeNavState({ mode: 'sage', setConversationId }), store)

    expect(store.list('sage')).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: /new conversation/i }))

    const sagConvs = store.list('sage')
    expect(sagConvs).toHaveLength(1)
    expect(setConversationId).toHaveBeenCalledWith(sagConvs[0].id)
  })

  it('marks the active conversation as aria-pressed=true', () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', 'Dragon Lore')

    renderLeftNav(makeNavState({ conversationId: conv.id }), store)

    const btn = screen.getByText('Dragon Lore').closest('button')
    expect(btn).toHaveAttribute('aria-pressed', 'true')
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
