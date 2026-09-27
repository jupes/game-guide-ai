/**
 * ChatPane in the GM channel (1kg.3.4): the thread is hydrated from the typed
 * timeline, renders three lanes, and draws a reloaded turn exactly as it drew
 * the live one. Sage/Spell/Rules keep their own suite (ChatPane.test.tsx).
 */

import { describe, it, expect, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
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
import type { ChatPaneProps } from './ChatPane'
import type { ChatMode, ChatResponse, StoredMessage } from '../api'
import type { Exchange, LoadHistoryFn, PostFn } from '../useChat'
import type { LoadTimelinePageFn } from '../gm/gmTimeline'
import { CREATIVE_ANSWER, chatEntry, pagedTimeline } from '../gm/threadFixtures'

// Review M-3: what the Export button hands to the download, without a download.
const exportChat = vi.hoisted(() => vi.fn<(exchanges: Exchange[]) => void>())
vi.mock('../exportChat', () => ({ exportChat }))

function navState(overrides: Partial<AppNavState>): AppNavState {
  return {
    screen: 'workspace',
    mode: 'gm',
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

const USER: CurrentUserContextValue = {
  user: { id: 'gm-1', displayName: 'Game Master', initials: 'GM', role: 'dm', signOut: vi.fn(), editProfile: vi.fn() },
  authStatus: 'authenticated',
  retryAuthCheck: vi.fn(),
  signIn: vi.fn(),
  setDisplayName: vi.fn(),
  setAvatarTone: vi.fn(),
}

const store = new MemoryConversationStore()
const noAttachments: ChatPaneProps['getAttachments'] = async () => ({ kind: 'ok', attachments: [] })

function Pane({ nav, ...props }: ChatPaneProps & { nav: Partial<AppNavState> }): React.JSX.Element {
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={navState(nav)}>
        <CurrentUserContext.Provider value={USER}>
          <ConversationStoreProvider store={store}>
            <ChatPane getAttachments={noAttachments} {...props} />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>
  )
}

/** The same answer the timeline fixture stores, as `/chat` returns it live. */
const LIVE: ChatResponse = {
  answer: CREATIVE_ANSWER.text,
  sources: [],
  answerable: false,
  stat_block: { ...CREATIVE_ANSWER.stat_block },
}

const PROMPT = 'Give me a drowned guardian for the marsh.'

describe('ChatPane (GM) — history hydration matches live rendering', () => {
  it('draws a reloaded turn with exactly the markup it had when it arrived', async () => {
    const post: PostFn = async () => ({ kind: 'ok', response: LIVE })
    const live = render(<Pane nav={{}} post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), PROMPT)
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(live.container.querySelector('.assistant-lane[data-state="done"]')).not.toBeNull())
    const liveMarkup = live.container.querySelector('.gm-thread__exchange')?.outerHTML
    live.unmount()

    const hydrated = render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={pagedTimeline([[chatEntry()]])} />)
    await waitFor(() => expect(hydrated.container.querySelector('.gm-thread__exchange')).not.toBeNull())
    expect(hydrated.container.querySelector('.gm-thread__exchange')?.outerHTML).toBe(liveMarkup)
    expect(liveMarkup).toContain('Creative — may include invented content')
  })
})

describe('ChatPane (GM) — reading the timeline', () => {
  it('keeps paging through an empty page, and shows what lay behind it', async () => {
    const load = pagedTimeline([[], [chatEntry({ entry_id: 'ent_old', prompt: 'An older question' })]])
    render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={load} />)
    expect(await screen.findByText('An older question')).toBeInTheDocument()
    expect(load.cursors).toEqual([null, 'p1'])
    expect(screen.queryByText('Ask the Game Master…')).toBeNull()
  })

  it('shows the recalling status while the timeline loads', async () => {
    const never: LoadTimelinePageFn = () => new Promise(() => {})
    render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={never} />)
    // Past the moment useChat's own (skipped) recall settles: the status must
    // follow the timeline, which is still out.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    const transcript = screen.getByRole('region', { name: 'Conversation' })
    expect(transcript).toHaveTextContent('Recalling the conversation…')
    expect(screen.queryByText('Ask the Game Master…')).toBeNull()
  })

  it('shows a conversation the server has not seen yet as an empty GM thread', async () => {
    render(<Pane nav={{ conversationId: 'cnv_new' }} loadTimeline={async () => ({ kind: 'missing' })} />)
    expect(await screen.findByText('Ask the Game Master…')).toBeInTheDocument()
    expect(screen.queryByText(/unavailable/i)).toBeNull()
  })

  it('puts a failed read above a thread that still works (§12.2)', async () => {
    const post: PostFn = async () => ({ kind: 'ok', response: LIVE })
    render(
      <Pane
        nav={{ conversationId: 'cnv_1' }}
        post={post}
        loadTimeline={async () => ({ kind: 'error', message: 'Message history unavailable (503).' })}
      />,
    )
    expect(await screen.findByText('Message history unavailable (503).')).toBeInTheDocument()
    await userEvent.type(screen.getByPlaceholderText('Ask…'), PROMPT)
    await userEvent.keyboard('{Enter}')
    expect(await screen.findByText('drowned guardian')).toBeInTheDocument()
  })

  it('reads GM history from the timeline only, and other channels never from it', async () => {
    const loadHistory = vi.fn<LoadHistoryFn>(async () => ({ kind: 'ok', messages: [] }))
    const loadTimeline = vi.fn<LoadTimelinePageFn>(async () => ({ kind: 'missing' }))
    const { rerender } = render(<Pane nav={{ conversationId: 'cnv_1' }} loadHistory={loadHistory} loadTimeline={loadTimeline} />)
    await waitFor(() => expect(loadTimeline).toHaveBeenCalled())
    expect(loadHistory).not.toHaveBeenCalled()

    loadTimeline.mockClear()
    rerender(<Pane nav={{ mode: 'sage', conversationId: 'cnv_1' }} loadHistory={loadHistory} loadTimeline={loadTimeline} />)
    await waitFor(() => expect(loadHistory).toHaveBeenCalledWith('cnv_1'))
    expect(loadTimeline).not.toHaveBeenCalled()
  })
})

describe('ChatPane (GM) — switching channel in the same conversation', () => {
  it('draws each turn once when moving from Sage into the GM channel', async () => {
    const loadHistory: LoadHistoryFn = async () => ({
      kind: 'ok',
      messages: [
        { id: 1, role: 'user', content: PROMPT, mode: 'sage', created_at: '2026-09-16T19:24:40Z' },
        { id: 2, role: 'assistant', content: 'An answer.', mode: 'sage', created_at: '2026-09-16T19:24:52Z' },
      ],
    })
    const loadTimeline = pagedTimeline([[chatEntry()]])
    const { rerender } = render(<Pane nav={{ mode: 'sage', conversationId: 'cnv_1' }} loadHistory={loadHistory} loadTimeline={loadTimeline} />)
    expect(await screen.findByText(PROMPT)).toBeInTheDocument()

    rerender(<Pane nav={{ mode: 'gm', conversationId: 'cnv_1' }} loadHistory={loadHistory} loadTimeline={loadTimeline} />)
    await waitFor(() => expect(document.querySelector('.gm-thread__exchange')).not.toBeNull())
    expect(screen.getAllByText(PROMPT)).toHaveLength(1)
  })
})

/** True when `a` comes before `b` in document order. */
function precedes(a: Node, b: Node): boolean {
  return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
}

const STORED_PROMPT = 'Who holds the lighthouse key?'
const storedThread = (): LoadTimelinePageFn =>
  pagedTimeline([[chatEntry({ entry_id: 'ent_old', prompt: STORED_PROMPT })]])

describe('ChatPane (GM) — stored history and turns sent since', () => {
  it('draws the stored history above a turn sent after it opened (review M-4)', async () => {
    const post: PostFn = async () => ({ kind: 'ok', response: LIVE })
    const { container } = render(<Pane nav={{ conversationId: 'cnv_1' }} post={post} loadTimeline={storedThread()} />)
    expect(await screen.findByText(STORED_PROMPT)).toBeInTheDocument()

    await userEvent.type(screen.getByPlaceholderText('Ask…'), PROMPT)
    await userEvent.keyboard('{Enter}')
    const live = await screen.findByText(PROMPT)

    expect(precedes(screen.getByText(STORED_PROMPT), live)).toBe(true)
    const drawn = [...container.querySelectorAll('.gm-thread__exchange')]
    expect(drawn).toHaveLength(2)
    expect(drawn[0]).toHaveTextContent(STORED_PROMPT)
    expect(drawn[1]).toHaveTextContent(PROMPT)
  })

  it('exports the stored history, then the turns sent since (review M-3)', async () => {
    exportChat.mockClear()
    const post: PostFn = async () => ({ kind: 'ok', response: LIVE })
    const { container } = render(<Pane nav={{ conversationId: 'cnv_1' }} post={post} loadTimeline={storedThread()} />)
    expect(await screen.findByText(STORED_PROMPT)).toBeInTheDocument()
    await userEvent.type(screen.getByPlaceholderText('Ask…'), PROMPT)
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(container.querySelectorAll('.assistant-lane[data-state="done"]')).toHaveLength(2))

    await userEvent.click(screen.getByRole('button', { name: 'Export chat' }))

    expect(exportChat).toHaveBeenCalledTimes(1)
    const [exported] = exportChat.mock.calls[0]
    expect(exported.map((e) => e.prompt)).toEqual([STORED_PROMPT, PROMPT])
    expect(exported[0].response?.answer).toBe(CREATIVE_ANSWER.text)
  })
})

/**
 * The service stores a turn, both halves, only once its answer is back
 * (`_persist_turn`), and every channel's history reads the same conversation.
 * `answerNext` lets the oldest turn in flight come back.
 */
function fakeService(answerText: string) {
  const stored: { prompt: string; mode: ChatMode }[] = []
  const inFlight: (() => void)[] = []
  const posts: [string, ChatMode, string | null][] = []
  const post: PostFn = (prompt, mode, conversationId) => {
    posts.push([prompt, mode, conversationId])
    return new Promise((resolve) => {
      inFlight.push(() => {
        stored.push({ prompt, mode })
        resolve({ kind: 'ok', response: { answer: answerText, sources: [], answerable: true } })
      })
    })
  }
  const loadHistory: LoadHistoryFn = async () => ({
    kind: 'ok',
    messages: stored.flatMap(({ prompt, mode }, i): StoredMessage[] => [
      { id: i * 2 + 1, role: 'user', content: prompt, mode, created_at: '2026-09-27T10:00:00Z' },
      { id: i * 2 + 2, role: 'assistant', content: answerText, mode, created_at: '2026-09-27T10:00:09Z' },
    ]),
  })
  const loadTimeline: LoadTimelinePageFn = async (conversationId) => ({
    kind: 'ok',
    page: {
      conversation_id: conversationId,
      items: stored
        .map(({ prompt, mode }, i) =>
          chatEntry({ entry_id: `ent_${i}`, mode, prompt, answer: { text: answerText, answerable: true, sources: [] } }),
        )
        .reverse(),
      next_cursor: null,
    },
  })
  return { post, loadHistory, loadTimeline, posts, answerNext: () => inFlight.shift()?.() }
}

describe('ChatPane — a turn in flight when the channel crosses the GM boundary (review M-1)', () => {
  const Q = 'Q-PROBE'

  it('keeps a Sage turn where it was sent until it settles, then crosses into GM with it stored', async () => {
    const service = fakeService('A stone-eyed lizard.')
    const pane = (mode: ChatMode) => (
      <Pane
        nav={{ mode, conversationId: 'cnv_1' }}
        post={service.post}
        loadHistory={service.loadHistory}
        loadTimeline={service.loadTimeline}
      />
    )
    const { rerender } = render(pane('sage'))
    await userEvent.type(screen.getByPlaceholderText('Ask…'), Q)
    await userEvent.keyboard('{Enter}')

    rerender(pane('gm'))
    // Not dropped: still drawn, still in flight, and the composer still locked on it…
    expect(screen.getByText(Q)).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Ask…')).toBeDisabled()
    // …and never drawn in the GM lanes while it is a Sage turn in flight.
    expect(document.querySelector('.gm-thread__exchange')).toBeNull()

    await act(async () => service.answerNext())
    await waitFor(() => expect(document.querySelector('.gm-thread__exchange')).not.toBeNull())
    expect(screen.getByText('A stone-eyed lizard.')).toBeInTheDocument()
    expect(screen.getAllByText(Q)).toHaveLength(1)
    expect(screen.getByPlaceholderText('Ask…')).toBeEnabled()
    expect(service.posts).toEqual([[Q, 'sage', 'cnv_1']])
  })

  it('keeps a GM turn where it was sent until it settles, then crosses into Sage with it stored', async () => {
    const service = fakeService('The innkeeper is a retired sapper.')
    const pane = (mode: ChatMode) => (
      <Pane
        nav={{ mode, conversationId: 'cnv_1' }}
        post={service.post}
        loadHistory={service.loadHistory}
        loadTimeline={service.loadTimeline}
      />
    )
    const { rerender } = render(pane('gm'))
    expect(await screen.findByText('Ask the Game Master…')).toBeInTheDocument()
    await userEvent.type(screen.getByPlaceholderText('Ask…'), Q)
    await userEvent.keyboard('{Enter}')

    rerender(pane('sage'))
    expect(screen.getByText(Q)).toBeInTheDocument()
    expect(document.querySelector('.assistant-lane')).toHaveAttribute('data-state', 'working')
    expect(screen.getByPlaceholderText('Ask…')).toBeDisabled()

    await act(async () => service.answerNext())
    await waitFor(() => expect(document.querySelector('.gm-thread__exchange')).toBeNull())
    expect(await screen.findByText('The innkeeper is a retired sapper.')).toBeInTheDocument()
    expect(screen.getAllByText(Q)).toHaveLength(1)
    expect(screen.getByPlaceholderText('Ask…')).toBeEnabled()
    expect(service.posts).toEqual([[Q, 'gm', 'cnv_1']])
  })
})
