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
import type { ChatResult, MessagesResult } from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'

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

function Wrapper({
  navState,
  post,
  loadHistory,
}: {
  navState?: Partial<AppNavState>
  post?: PostFn
  loadHistory?: LoadHistoryFn
}): React.JSX.Element {
  const store = new MemoryConversationStore()
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={makeNavState(navState)}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <ConversationStoreProvider store={store}>
            <ChatPane post={post} loadHistory={loadHistory} />
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

  it('shows a creative marker for GM answers that are not grounded (answerable=false)', async () => {
    const creative: ChatResult = {
      kind: 'ok',
      response: {
        answer: 'The swamp hides a Mire Crone, a hag of my own devising.',
        sources: [],
        answerable: false,
      },
    }
    const post: PostFn = async () => creative
    render(<Wrapper navState={{ mode: 'gm' }} post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'Invent a swamp monster')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByText(/may include invented content/i)).toBeInTheDocument())
  })

  it('does not show the creative marker for grounded sage answers', async () => {
    const post: PostFn = async () => GROUNDED
    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What is a Basilisk?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )
    expect(screen.queryByText(/may include invented content/i)).not.toBeInTheDocument()
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

  // ── channel-chats CP-B — history recall ─────────────────────────────────────

  it('renders recalled history when a conversation opens', async () => {
    const loadHistory: LoadHistoryFn = async () => ({
      kind: 'ok',
      messages: [
        { id: 1, role: 'user', content: 'What is a goblin?', mode: 'sage', created_at: '2026-07-08T12:00:00Z' },
        { id: 2, role: 'assistant', content: 'A small green menace.', mode: 'sage', created_at: '2026-07-08T12:00:01Z' },
      ],
    })
    render(<Wrapper navState={{ conversationId: 'conv-1' }} loadHistory={loadHistory} />)

    await waitFor(() => expect(screen.getByText('What is a goblin?')).toBeInTheDocument())
    expect(screen.getByText('A small green menace.')).toBeInTheDocument()
    // The mode empty-state must not show under recalled history.
    expect(screen.queryByText('Ask the Sage…')).not.toBeInTheDocument()
  })

  it('shows a notice when history recall fails, composer still usable', async () => {
    const loadHistory: LoadHistoryFn = async () => ({
      kind: 'error',
      message: 'Message history unavailable (503).',
    })
    const post: PostFn = async () => GROUNDED
    render(<Wrapper navState={{ conversationId: 'conv-1' }} post={post} loadHistory={loadHistory} />)

    await waitFor(() =>
      expect(screen.getByText(/message history unavailable/i)).toBeInTheDocument(),
    )
    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'Still works?')
    await userEvent.keyboard('{Enter}')
    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )
  })

  // ── channel-chats CP-C — spell suggestion cards ────────────────────────────

  const SUGGESTIONS = [
    { style: 'practical' as const, text: 'Clear a room of enemies.' },
    { style: 'roleplay' as const, text: 'Light the beacon at the festival.' },
    { style: 'wacky' as const, text: 'Instantly roast a feast.' },
  ]

  it('renders three labeled suggestion cards under a spell answer', async () => {
    const post: PostFn = async () => ({
      kind: 'ok',
      response: {
        answer: 'Fireball: 8d6 fire damage in a 20-foot radius.',
        sources: [],
        answerable: true,
        suggestions: SUGGESTIONS,
      },
    })
    render(<Wrapper navState={{ mode: 'spell' }} post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What does Fireball do?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByText(/8d6 fire damage/)).toBeInTheDocument())
    expect(screen.getByText('Practical')).toBeInTheDocument()
    expect(screen.getByText('Roleplay')).toBeInTheDocument()
    expect(screen.getByText('Wacky')).toBeInTheDocument()
    expect(screen.getByText('Clear a room of enemies.')).toBeInTheDocument()
    expect(screen.getByText('Instantly roast a feast.')).toBeInTheDocument()
  })

  it('renders no suggestion cards when the response has none', async () => {
    const post: PostFn = async () => GROUNDED
    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What is a Basilisk?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )
    expect(screen.queryByText('Practical')).not.toBeInTheDocument()
  })

  it('renders suggestion cards on recalled spell history', async () => {
    const loadHistory: LoadHistoryFn = async () => ({
      kind: 'ok',
      messages: [
        { id: 1, role: 'user', content: 'What does Fireball do?', mode: 'spell', created_at: '2026-07-08T12:00:00Z' },
        {
          id: 2,
          role: 'assistant',
          content: 'Fireball: 8d6 fire damage.',
          mode: 'spell',
          created_at: '2026-07-08T12:00:01Z',
          suggestions: SUGGESTIONS,
        },
      ],
    })
    render(<Wrapper navState={{ mode: 'spell', conversationId: 'conv-1' }} loadHistory={loadHistory} />)

    await waitFor(() => expect(screen.getByText('Fireball: 8d6 fire damage.')).toBeInTheDocument())
    expect(screen.getByText('Practical')).toBeInTheDocument()
    expect(screen.getByText('Light the beacon at the festival.')).toBeInTheDocument()
  })

  it('shows a recall status while history loads', async () => {
    let resolveHistory!: (r: MessagesResult) => void
    const loadHistory: LoadHistoryFn = () =>
      new Promise<MessagesResult>((res) => {
        resolveHistory = res
      })
    render(<Wrapper navState={{ conversationId: 'conv-1' }} loadHistory={loadHistory} />)

    expect(screen.getByText(/recalling/i)).toBeInTheDocument()
    act(() => resolveHistory({ kind: 'ok', messages: [] }))
    await waitFor(() => expect(screen.queryByText(/recalling/i)).not.toBeInTheDocument())
    expect(screen.getByText('Ask the Sage…')).toBeInTheDocument()
  })
})
