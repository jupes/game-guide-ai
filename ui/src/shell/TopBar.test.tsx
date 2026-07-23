import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { LeftNav } from './LeftNav'
import { MemoryConversationStore } from './conversationStore'
import { TopBar } from './TopBar'

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

function makeUserState(): CurrentUserContextValue {
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
  }
}

function renderChrome(
  store: MemoryConversationStore,
  nav: AppNavState,
): void {
  render(
    <ThemeProvider>
      <AppNavContext.Provider value={nav}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <ConversationStoreProvider store={store}>
            <TopBar />
            <LeftNav />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>,
  )
}

describe('TopBar active conversation title', () => {
  it('resolves AppNav active selection through the conversation store', () => {
    const store = new MemoryConversationStore()
    store.create('sage', 'Inactive conversation')
    const active = store.create('sage', 'Basilisk lore')

    render(
      <AppNavContext.Provider
        value={makeNavState({ conversationId: active.id })}
      >
        <ConversationStoreProvider store={store}>
          <TopBar />
        </ConversationStoreProvider>
      </AppNavContext.Provider>,
    )

    expect(screen.getByText('Basilisk lore')).toBeInTheDocument()
    expect(screen.queryByText('Inactive conversation')).not.toBeInTheDocument()
  })

  it('reacts with the conversation list when an inline rename is saved', async () => {
    const store = new MemoryConversationStore()
    const active = store.create('sage', 'Basilisk lore')
    const nav = makeNavState({ conversationId: active.id })

    renderChrome(store, nav)

    expect(screen.getAllByText('Basilisk lore')).toHaveLength(2)
    await userEvent.click(
      screen.getByRole('button', { name: 'Rename Basilisk lore' }),
    )
    const input = screen.getByRole('textbox', { name: /conversation title/i })
    await userEvent.clear(input)
    await userEvent.type(input, '  Monster research  ')
    await userEvent.keyboard('{Enter}')

    expect(screen.getAllByText('Monster research')).toHaveLength(2)
    expect(store.get(active.id)?.title).toBe('Monster research')
  })

  it('cancels an inline rename with Escape', async () => {
    const store = new MemoryConversationStore()
    const active = store.create('sage', 'Basilisk lore')
    renderChrome(store, makeNavState({ conversationId: active.id }))

    await userEvent.click(
      screen.getByRole('button', { name: 'Rename Basilisk lore' }),
    )
    const input = screen.getByRole('textbox', { name: /conversation title/i })
    await userEvent.clear(input)
    await userEvent.type(input, 'Discard this{Escape}')

    expect(screen.queryByRole('textbox', { name: /conversation title/i }))
      .not.toBeInTheDocument()
    expect(screen.getAllByText('Basilisk lore')).toHaveLength(2)
    expect(store.get(active.id)?.title).toBe('Basilisk lore')
  })

  it('saves on blur and restores the derived title when cleared', async () => {
    const store = new MemoryConversationStore()
    const active = store.create('sage', 'Basilisk lore')
    renderChrome(store, makeNavState({ conversationId: active.id }))

    await userEvent.click(
      screen.getByRole('button', { name: 'Rename Basilisk lore' }),
    )
    let input = screen.getByRole('textbox', { name: /conversation title/i })
    await userEvent.clear(input)
    await userEvent.type(input, '  Monster research  ')
    await userEvent.tab()
    expect(screen.getAllByText('Monster research')).toHaveLength(2)

    await userEvent.click(
      screen.getByRole('button', { name: 'Rename Monster research' }),
    )
    input = screen.getByRole('textbox', { name: /conversation title/i })
    await userEvent.clear(input)
    await userEvent.tab()

    expect(screen.getAllByText('Basilisk lore')).toHaveLength(2)
    expect(store.get(active.id)?.title).toBe('Basilisk lore')
  })
})
