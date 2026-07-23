import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
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
  // channel-chats CP-D: the GM channel is DM-only, so the mode list is
  // role-aware — 4 labels for a dm, 3 for a player.
  it('renders all 4 mode labels for a dm-role user', () => {
    const navState = makeNavState()
    const userState = makeUserState('dm')
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

  it('hides the GM mode chip from a player-role user', () => {
    const navState = makeNavState()
    const userState = makeUserState('player')
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
    expect(screen.queryByText('GM')).not.toBeInTheDocument()
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

  // swe1.3 — each channel chip carries its distinct accent class so the
  // selected fill / icon tint differs per mode.
  it('applies each mode its accent modifier class', () => {
    const navState = makeNavState({ mode: 'spell' })
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={navState}>
          <CurrentUserContext.Provider value={makeUserState('dm')}>
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <LeftNav />
            </ConversationStoreProvider>
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    expect(screen.getByText('Sage').closest('.chip')).toHaveClass('mode-accent--verdigris')
    const spellChip = screen.getByText('Spell').closest('.chip')
    expect(spellChip).toHaveClass('mode-accent--arcane')
    expect(spellChip).toHaveClass('chip--selected') // active mode fills with its accent
    expect(screen.getByText('Rules').closest('.chip')).toHaveClass('mode-accent--gold')
    expect(screen.getByText('GM').closest('.chip')).toHaveClass('mode-accent--ember')
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

// ── swe1.2 — selection is applied and visibly distinct ───────────────────────
// The mocked-callback tests above prove LeftNav *calls* setConversationId, but
// the reported bug was that selecting a conversation showed no visual change
// (the --selected style resolved to an undefined token). These tests drive real
// nav state so the selected class actually toggles, covering the click and
// keyboard (Enter/Space) paths end to end within LeftNav.

const SELECTED = 'left-nav__conversation--selected'

/** LeftNav wired to real conversationId state (not a vi.fn). */
function StatefulLeftNav({
  store,
  initialId = null,
}: {
  store: MemoryConversationStore
  initialId?: string | null
}) {
  const [conversationId, setConversationId] = useState<string | null>(initialId)
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={makeNavState({ conversationId, setConversationId })}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <ConversationStoreProvider store={store}>
            <LeftNav />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>
  )
}

describe('LeftNav conversation selection (swe1.2)', () => {
  function twoConversations() {
    const store = new MemoryConversationStore()
    store.create('sage', 'Dragon Lore')
    store.create('sage', 'Basilisk Info')
    return store
  }

  const rowFor = (title: string) => screen.getByText(title).closest('button')

  it('applies the selected style + aria-pressed to the clicked row only', async () => {
    render(<StatefulLeftNav store={twoConversations()} />)

    // Nothing selected before a click.
    expect(rowFor('Dragon Lore')).not.toHaveClass(SELECTED)
    expect(rowFor('Basilisk Info')).not.toHaveClass(SELECTED)

    await userEvent.click(screen.getByText('Dragon Lore'))

    expect(rowFor('Dragon Lore')).toHaveClass(SELECTED)
    expect(rowFor('Dragon Lore')).toHaveAttribute('aria-pressed', 'true')
    expect(rowFor('Basilisk Info')).not.toHaveClass(SELECTED)
    expect(rowFor('Basilisk Info')).toHaveAttribute('aria-pressed', 'false')
  })

  it('moves the selected style when a different row is clicked', async () => {
    render(<StatefulLeftNav store={twoConversations()} />)

    await userEvent.click(screen.getByText('Dragon Lore'))
    await userEvent.click(screen.getByText('Basilisk Info'))

    expect(rowFor('Basilisk Info')).toHaveClass(SELECTED)
    expect(rowFor('Dragon Lore')).not.toHaveClass(SELECTED)
  })

  it('selects via keyboard — Enter and Space activate a focused row', async () => {
    render(<StatefulLeftNav store={twoConversations()} />)

    rowFor('Dragon Lore')!.focus()
    await userEvent.keyboard('{Enter}')
    expect(rowFor('Dragon Lore')).toHaveClass(SELECTED)

    rowFor('Basilisk Info')!.focus()
    await userEvent.keyboard(' ')
    expect(rowFor('Basilisk Info')).toHaveClass(SELECTED)
    expect(rowFor('Dragon Lore')).not.toHaveClass(SELECTED)
  })
})

// ── swe1.10 — brand appears once in the workspace chrome ─────────────────────

describe('workspace chrome brand (swe1.10)', () => {
  it('renders "Aetheril" exactly once across TopBar + LeftNav', () => {
    render(
      <ThemeProvider>
        <AppNavContext.Provider value={makeNavState()}>
          <CurrentUserContext.Provider value={makeUserState()}>
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <TopBar />
              <LeftNav />
            </ConversationStoreProvider>
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )
    expect(screen.getAllByText('Aetheril')).toHaveLength(1)
  })
})

// ── TopBar — theme control belongs to AppHeader ───────────────────────────────

describe('TopBar theme ownership (eiio.3)', () => {
  it('does not render the theme switch owned by AppHeader', () => {
    render(
      <ThemeProvider initialTheme="light">
        <TopBar />
      </ThemeProvider>,
    )
    expect(screen.queryByRole('switch', { name: /dark theme/i })).not.toBeInTheDocument()
  })
})
