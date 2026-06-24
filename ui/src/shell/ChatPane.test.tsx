import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as React from 'react'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { ThemeProvider } from '../ds/theme'
import { ChatPane } from './ChatPane'
import type { ChatResult } from '../api'
import type { PostFn } from '../useChat'

// ── CP-F5.3 — ChatPane behaviors (#21) ────────────────────────────────────────

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

function Wrapper({
  navState,
  post,
}: {
  navState?: Partial<AppNavState>
  post?: PostFn
}): React.JSX.Element {
  const store = new MemoryConversationStore()
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={makeNavState(navState)}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <ConversationStoreProvider store={store}>
            <ChatPane post={post} />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>
  )
}

const GROUNDED: ChatResult = {
  kind: 'ok',
  response: {
    answer: 'A basilisk petrifies with its gaze.',
    sources: [
      {
        book: 'mm-5e',
        chapter: 'Bestiary',
        section: 'Stat Block',
        entity: 'Basilisk',
        page: 12,
        snippet: 'Armor Class 15 …',
      },
    ],
    answerable: true,
  },
}

describe('ChatPane (#21)', () => {
  it('shows the mode-aware empty state when no exchanges exist', () => {
    render(<Wrapper />)
    expect(screen.getByText('Ask the Sage…')).toBeInTheDocument()
  })

  it('shows spell archivist label for spell mode', () => {
    render(<Wrapper navState={{ mode: 'spell' }} />)
    expect(screen.getByText('Ask the Spell Archivist…')).toBeInTheDocument()
  })

  it('submits a prompt and renders a player ChatMessage', async () => {
    let resolvePost!: (r: ChatResult) => void
    const post: PostFn = () =>
      new Promise<ChatResult>((res) => {
        resolvePost = res
      })

    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What is a Basilisk?')
    await userEvent.keyboard('{Enter}')

    expect(screen.getByText('What is a Basilisk?')).toBeInTheDocument()

    // Resolve the post so the test can clean up
    act(() => resolvePost(GROUNDED))
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
  })

  it('renders a dm ChatMessage with the answer after post resolves', async () => {
    const post: PostFn = async () => GROUNDED
    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What is a Basilisk?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )
  })

  it('shows a pending status while waiting for a response', async () => {
    let resolvePost!: (r: ChatResult) => void
    const post: PostFn = () =>
      new Promise<ChatResult>((res) => {
        resolvePost = res
      })

    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'Q')
    await userEvent.keyboard('{Enter}')

    expect(screen.getByRole('status')).toHaveTextContent(/consulting the tomes/i)

    act(() => resolvePost(GROUNDED))
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
  })

  it('renders sources in a Card after the answer', async () => {
    const post: PostFn = async () => GROUNDED
    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What is a Basilisk?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByText(/1 source/i)).toBeInTheDocument())
    // The source count badge is inside a Card with the outlined variant
    const sourceEl = screen.getByText(/1 source/i)
    expect(sourceEl.closest('.card--outlined')).not.toBeNull()
  })

  it('renders the export button', () => {
    render(<Wrapper />)
    expect(screen.getByRole('button', { name: /export/i })).toBeInTheDocument()
  })

  it('renders an error message in a system ChatMessage', async () => {
    const post: PostFn = async () => ({ kind: 'error', message: 'Service unavailable' })
    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'Q')
    await userEvent.keyboard('{Enter}')

    await waitFor(() =>
      expect(screen.getByText(/service unavailable/i)).toBeInTheDocument(),
    )
  })
})
