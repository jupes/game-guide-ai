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
import type { Attachment, AttachmentsResult, ChatResult, MessagesResult, UploadAttachmentResult } from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'
import type { GetAttachmentsFn, UploadAttachmentFn } from './ChatPane'

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
    openProfile: vi.fn(),
    backToWorkspace: vi.fn(),
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
    authStatus: 'authenticated',
    retryAuthCheck: vi.fn(),
    signIn: vi.fn(),
    setDisplayName: vi.fn(),
    setAvatarTone: vi.fn(),
    ...overrides,
  }
}

function Wrapper({
  navState,
  post,
  loadHistory,
  uploadAttachment,
  getAttachments,
  store = new MemoryConversationStore(),
}: {
  navState?: Partial<AppNavState>
  post?: PostFn
  loadHistory?: LoadHistoryFn
  uploadAttachment?: UploadAttachmentFn
  getAttachments?: GetAttachmentsFn
  store?: MemoryConversationStore
}): React.JSX.Element {
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={makeNavState(navState)}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <ConversationStoreProvider store={store}>
            <ChatPane
              post={post}
              loadHistory={loadHistory}
              uploadAttachment={uploadAttachment}
              getAttachments={getAttachments}
            />
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

describe('ChatPane — markdown rendering (pp6q.1.1)', () => {
  it('renders a markdown answer as formatted HTML, not a raw string', async () => {
    const post: PostFn = async () => ({
      kind: 'ok',
      response: {
        answer: '## Fireball\n\nA **bright streak** flashes.\n\n- Dex save\n- Half on success',
        sources: [],
        answerable: true,
      },
    })
    render(<Wrapper post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'Fireball?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Fireball' })).toBeInTheDocument())
    expect(screen.getByText('bright streak').tagName).toBe('STRONG')
    expect(screen.getAllByRole('listitem').map((li) => li.textContent))
      .toEqual(['Dex save', 'Half on success'])
    // The literal markdown syntax must not survive as visible text.
    expect(screen.queryByText(/## Fireball/)).toBeNull()
  })
})

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

  it('records the first submitted prompt as the active conversation title fallback', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    const emptyHistory: LoadHistoryFn = async () => ({ kind: 'ok', messages: [] })
    const noAttachments: GetAttachmentsFn = async () => ({ kind: 'ok', attachments: [] })

    render(
      <Wrapper
        navState={{ conversationId: conv.id }}
        store={store}
        post={async () => GROUNDED}
        loadHistory={emptyHistory}
        getAttachments={noAttachments}
      />,
    )

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'What is a basilisk?')
    await userEvent.keyboard('{Enter}')

    expect(store.get(conv.id)?.title).toBe('What is a basilisk?')
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

  // ── swe1.6 — file attachments ────────────────────────────────────────────

  const ATTACHMENT: Attachment = {
    id: 1, filename: 'notes.txt', content_type: 'text/plain', chars: 12,
    created_at: '2026-07-20T12:00:00Z',
  }

  // conversationId is set in all three specs below (an attachment needs a
  // conversation to belong to), which means useChat's history-recall effect
  // fires too — supply a no-op loadHistory so it doesn't hit the real network.
  const emptyHistory: LoadHistoryFn = async () => ({ kind: 'ok', messages: [] })

  it('shows a conversation\'s existing attachments on load', async () => {
    const getAttachments: GetAttachmentsFn = async (): Promise<AttachmentsResult> => ({
      kind: 'ok', attachments: [ATTACHMENT],
    })
    render(
      <Wrapper
        navState={{ conversationId: 'conv-1' }}
        loadHistory={emptyHistory}
        getAttachments={getAttachments}
      />,
    )
    await waitFor(() => expect(screen.getByText('notes.txt')).toBeInTheDocument())
  })

  it('uploads a file via the attach button and shows it as a chip', async () => {
    let resolveUpload!: (r: UploadAttachmentResult) => void
    const uploadAttachment: UploadAttachmentFn = () =>
      new Promise<UploadAttachmentResult>((res) => {
        resolveUpload = res
      })
    render(
      <Wrapper
        navState={{ conversationId: 'conv-1' }}
        loadHistory={emptyHistory}
        uploadAttachment={uploadAttachment}
      />,
    )

    const input = screen.getByLabelText(/attach file/i, { selector: 'input' }) as HTMLInputElement
    const file = new File(['the orb is cursed'], 'notes.txt', { type: 'text/plain' })
    await userEvent.upload(input, file)

    act(() => resolveUpload({ kind: 'ok', attachment: ATTACHMENT }))
    await waitFor(() => expect(screen.getByText('notes.txt')).toBeInTheDocument())
  })

  it('surfaces an upload error without losing the composer', async () => {
    const uploadAttachment: UploadAttachmentFn = async () => ({
      kind: 'error', message: "That file type isn't supported.",
    })
    render(
      <Wrapper
        navState={{ conversationId: 'conv-1' }}
        loadHistory={emptyHistory}
        uploadAttachment={uploadAttachment}
      />,
    )

    const input = screen.getByLabelText(/attach file/i, { selector: 'input' }) as HTMLInputElement
    const file = new File(['x'], 'art.png', { type: 'image/png' })
    // applyAccept off: the picker's accept filter would block .png client-side;
    // this spec exercises the server-refusal path for a forced wrong file.
    await userEvent.upload(input, file, { applyAccept: false })

    await waitFor(() =>
      expect(screen.getByText(/isn't supported/i)).toBeInTheDocument(),
    )
    expect(screen.getByPlaceholderText('Ask…')).toBeInTheDocument()
  })

  it('pre-filters the picker to the supported attachment types', () => {
    render(
      <Wrapper navState={{ conversationId: 'conv-1' }} loadHistory={emptyHistory} />,
    )
    const input = screen.getByLabelText(/attach file/i, { selector: 'input' }) as HTMLInputElement
    expect(input.accept).toBe('.txt,.md,.pdf')
  })

  it('a rejecting upload surfaces an error instead of an unhandled rejection', async () => {
    const uploadAttachment: UploadAttachmentFn = () =>
      Promise.reject(new Error('wire snapped'))
    render(
      <Wrapper
        navState={{ conversationId: 'conv-1' }}
        loadHistory={emptyHistory}
        uploadAttachment={uploadAttachment}
      />,
    )

    const input = screen.getByLabelText(/attach file/i, { selector: 'input' }) as HTMLInputElement
    const file = new File(['the orb is cursed'], 'notes.txt', { type: 'text/plain' })
    await userEvent.upload(input, file)

    await waitFor(() =>
      expect(screen.getByText(/couldn't upload/i)).toBeInTheDocument(),
    )
    expect(screen.getByPlaceholderText('Ask…')).toBeInTheDocument()
  })

  it('a rejecting attachment load degrades to no chips without crashing', async () => {
    const getAttachments: GetAttachmentsFn = () =>
      Promise.reject(new Error('wire snapped'))
    render(
      <Wrapper
        navState={{ conversationId: 'conv-1' }}
        loadHistory={emptyHistory}
        getAttachments={getAttachments}
      />,
    )
    // Pane renders and stays interactive; no attachment chips appear.
    await waitFor(() => expect(screen.getByPlaceholderText('Ask…')).toBeInTheDocument())
    expect(screen.queryByText('notes.txt')).not.toBeInTheDocument()
  })

  // ── z7fl.4 Checkpoint E — structured content widgets ───────────────────────

  const SPELL_CONTENT = {
    name: 'Fireball',
    level: 3,
    school: 'evocation',
    casting_time: '1 action',
    range: '150 feet',
    duration: 'Instantaneous',
    components: { v: true, s: true, m: 'a tiny ball of bat guano and sulfur' },
    description: 'A bright streak flashes from you to a point you choose within range.',
    higher_levels: 'the damage increases by 1d6 for each slot level above 3rd',
    classes: ['Sorcerer', 'Wizard'],
  }

  const STAT_BLOCK = {
    name: 'Goblin Scout',
    size: 'Small',
    type: 'humanoid',
    alignment: 'neutral evil',
    ac: 15,
    hp: 7,
    traits: [{ name: 'Nimble Escape', text: 'The goblin can take the Disengage or Hide action as a bonus action.' }],
    actions: [{ name: 'Scimitar', text: 'Melee Weapon Attack: +4 to hit.' }],
  }

  it('renders a SpellCard alongside the prose bubble when spell_content is present (behavior 12)', async () => {
    // Additive, not a replacement (PR #46 review): the structuring call is a
    // separate, schema-constrained LLM extraction -- it's prompted not to
    // invent facts, but nothing guarantees it captures every fact either.
    // Hiding the prose risks silently dropping content outside the schema
    // (asides, caveats, anything that doesn't map to a SpellContent field).
    const post: PostFn = async () => ({
      kind: 'ok',
      response: {
        answer: 'Fireball: 8d6 fire damage in a 20-foot radius.',
        sources: [],
        answerable: true,
        spell_content: SPELL_CONTENT,
      },
    })
    render(<Wrapper navState={{ mode: 'spell' }} post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What does Fireball do?')
    await userEvent.keyboard('{Enter}')

    // The card's content is visible...
    await waitFor(() => expect(screen.getByText('Fireball')).toBeInTheDocument())
    expect(screen.getByText(/3rd-level evocation/)).toBeInTheDocument()
    expect(screen.getByText('a tiny ball of bat guano and sulfur')).toBeInTheDocument()
    expect(screen.getByText('the damage increases by 1d6 for each slot level above 3rd')).toBeInTheDocument()
    // ...and so is the original prose bubble (nothing the model said is hidden).
    expect(screen.getByText('Fireball: 8d6 fire damage in a 20-foot radius.')).toBeInTheDocument()
  })

  it('renders a StatBlockCard alongside the prose bubble when stat_block is present (behavior 13)', async () => {
    const post: PostFn = async () => ({
      kind: 'ok',
      response: {
        answer: 'You encounter a goblin scout. Armor Class 15. Hit Points 7.',
        sources: [],
        answerable: true,
        stat_block: STAT_BLOCK,
      },
    })
    render(<Wrapper navState={{ mode: 'gm' }} post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'Describe an encounter')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByText('Goblin Scout')).toBeInTheDocument())
    expect(screen.getByText('Small humanoid, neutral evil')).toBeInTheDocument()
    expect(screen.getByText(/Nimble Escape/)).toBeInTheDocument()
    expect(screen.getByText(/Scimitar/)).toBeInTheDocument()
    // The original prose bubble is also shown (nothing the model said is hidden).
    expect(
      screen.getByText('You encounter a goblin scout. Armor Class 15. Hit Points 7.'),
    ).toBeInTheDocument()
  })

  it('falls back to the plain prose bubble when neither spell_content nor stat_block is present (behavior 14)', async () => {
    const post: PostFn = async () => GROUNDED
    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What is a Basilisk?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )
  })

  it('keeps suggestion cards additive alongside a SpellCard in the same turn', async () => {
    const post: PostFn = async () => ({
      kind: 'ok',
      response: {
        answer: 'Fireball: 8d6 fire damage in a 20-foot radius.',
        sources: [],
        answerable: true,
        spell_content: SPELL_CONTENT,
        suggestions: SUGGESTIONS,
      },
    })
    render(<Wrapper navState={{ mode: 'spell' }} post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'What does Fireball do?')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByText('Fireball')).toBeInTheDocument())
    expect(screen.getByText('Practical')).toBeInTheDocument()
  })
})
