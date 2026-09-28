/**
 * ChatPane in the GM channel (1kg.3.4): the thread is hydrated from the typed
 * timeline, renders three lanes, and draws a reloaded turn exactly as it drew
 * the live one. Sage/Spell/Rules keep their own suite (ChatPane.test.tsx).
 */

import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
import type { ChatMode, ChatResponse, StoredMessage, TimelinePageResult } from '../api'
import type { Exchange, LoadHistoryFn, PostFn } from '../useChat'
import { HYDRATE_TARGET } from '../gm/gmTimeline'
import type { LoadTimelinePageFn } from '../gm/gmTimeline'
import { CREATIVE_ANSWER, chatEntry, manyChatEntries, pagedTimeline } from '../gm/threadFixtures'

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
    // Queried and asserted in one callback, never through a handle held across
    // an await: Markdown re-sets its innerHTML on the pane's next render (React
    // 19 re-applies `dangerouslySetInnerHTML` whenever the object is new), so
    // the <strong> findByText first matched can be detached by the time a
    // separate expect reads it (agent-forge-harness-57l).
    await waitFor(() => expect(screen.getByText('drowned guardian')).toBeInTheDocument())
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

describe('ChatPane (GM) — announcing a turn (review M-2)', () => {
  it('announces the start of a GM turn, then its arrival, on the one live region the pane already had', async () => {
    let answer: (result: { kind: 'ok'; response: ChatResponse }) => void = () => {}
    const post: PostFn = () => new Promise((resolve) => { answer = resolve })
    render(<Pane nav={{}} post={post} />)
    // Captured BEFORE the send (agent-forge-harness-4oz): the assertion is on a
    // node that was already mounted, never on one the pending render created.
    const announcer = screen.getByRole('status')

    await userEvent.type(screen.getByPlaceholderText('Ask…'), PROMPT)
    await userEvent.keyboard('{Enter}')
    expect(document.querySelector('.assistant-lane[data-state="working"]')).not.toBeNull()
    expect(announcer).toHaveTextContent('Consulting the tomes…')
    expect(screen.getAllByRole('status')).toEqual([announcer])

    await act(async () => answer({ kind: 'ok', response: LIVE }))
    expect(announcer).toHaveTextContent('Answer received')
    expect(screen.getAllByRole('status')).toEqual([announcer])
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

// jsdom performs NO layout — see ChatPane.test.tsx's own stubGeometry. This
// copy's scrollHeight is a getter/setter rather than a fixed value, so a test
// can simulate the DOM growing once the older turn actually renders.
function stubGeometry(
  el: Element,
  { scrollHeight, clientHeight, scrollTop }: { scrollHeight: number; clientHeight: number; scrollTop: number },
) {
  let height = scrollHeight
  let top = scrollTop
  Object.defineProperty(el, 'scrollHeight', { get: () => height, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
  Object.defineProperty(el, 'scrollTop', {
    get: () => top,
    set: (v: number) => {
      top = v
    },
    configurable: true,
  })
  return {
    get scrollTop() {
      return top
    },
    set scrollTop(v: number) {
      top = v
    },
    set scrollHeight(v: number) {
      height = v
    },
  }
}

describe('ChatPane (GM) — Load earlier (1kg.3.6)', () => {
  const OLDER_PROMPT = 'Who guards the old bridge?'

  it('offers Load earlier once the thread exceeds one page, and prepends without duplicating', async () => {
    const load = pagedTimeline([manyChatEntries(HYDRATE_TARGET, 9000), [chatEntry({ entry_id: 'ent_older', prompt: OLDER_PROMPT })]])
    const { container } = render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={load} />)
    await waitFor(() => expect(container.querySelectorAll('.gm-thread__exchange')).toHaveLength(HYDRATE_TARGET))
    const button = screen.getByRole('button', { name: 'Load earlier' })

    await userEvent.click(button)
    expect(await screen.findByText(OLDER_PROMPT)).toBeInTheDocument()

    const exchanges = container.querySelectorAll('.gm-thread__exchange')
    expect(exchanges).toHaveLength(HYDRATE_TARGET + 1)
    // Prepended, not appended — the older turn leads the thread — and drawn
    // exactly once, not twice over.
    expect(exchanges[0]).toHaveTextContent(OLDER_PROMPT)
    expect(screen.getAllByText(OLDER_PROMPT)).toHaveLength(1)
    // The list has ended — nothing left to load.
    expect(screen.queryByRole('button', { name: 'Load earlier' })).toBeNull()
  })

  it('walks through an empty older page to the turns behind it (review M4)', async () => {
    // A page may be empty while its cursor is not null — only a null cursor
    // ends the list, on the Load earlier path as on the first read.
    const load = pagedTimeline([
      manyChatEntries(HYDRATE_TARGET, 9400),
      [],
      [chatEntry({ entry_id: 'ent_behind_empty', prompt: OLDER_PROMPT })],
    ])
    const { container } = render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={load} />)
    await waitFor(() => expect(container.querySelectorAll('.gm-thread__exchange')).toHaveLength(HYDRATE_TARGET))

    await userEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    await waitFor(() => expect(screen.getByText(OLDER_PROMPT)).toBeInTheDocument())

    expect(load.cursors).toEqual([null, 'p1', 'p2'])
    expect(container.querySelectorAll('.gm-thread__exchange')[0]).toHaveTextContent(OLDER_PROMPT)
    expect(screen.queryByRole('button', { name: 'Load earlier' })).toBeNull()
  })

  it('announces the start of a Load earlier walk, then its outcome, on the one live region the pane already had', async () => {
    // A promise the test resolves explicitly (review M-2's own pattern,
    // above) — an in-memory fetch that settled on its own microtask could
    // beat `userEvent.click`'s own await to the punch and skip past the
    // starting announcement before this test ever gets to read it.
    let resolveOlder: ((result: TimelinePageResult) => void) | null = null
    const load: LoadTimelinePageFn = async (conversationId, cursor) => {
      if (cursor === null) {
        return { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 9100), next_cursor: 'p1' } }
      }
      return new Promise((resolve) => {
        resolveOlder = resolve
      })
    }
    render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={load} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load earlier' })).toBeInTheDocument())
    // Captured before the press, like the turn-announcement test above.
    const announcer = screen.getByRole('status')

    await userEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    expect(announcer).toHaveTextContent('Loading earlier turns…')
    expect(screen.getAllByRole('status')).toEqual([announcer])

    await act(async () => {
      resolveOlder?.({ kind: 'ok', page: { conversation_id: 'cnv_1', items: [chatEntry({ entry_id: 'ent_older2' })], next_cursor: null } })
    })
    expect(announcer).toHaveTextContent('Earlier turns loaded')
    expect(screen.getAllByRole('status')).toEqual([announcer])
  })

  it('retries in place after a failed walk, without losing what is already drawn (STATE-1)', async () => {
    let failNext = true
    const load = vi.fn<LoadTimelinePageFn>(async (conversationId, cursor) => {
      if (cursor === null) {
        return { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 9200), next_cursor: 'p1' } }
      }
      if (failNext) {
        failNext = false
        return { kind: 'error', message: 'Message history unavailable (503).' }
      }
      return { kind: 'ok', page: { conversation_id: conversationId, items: [chatEntry({ entry_id: 'ent_older3' })], next_cursor: null } }
    })
    const { container } = render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={load} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load earlier' })).toBeInTheDocument())
    const announcer = screen.getByRole('status')

    await userEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    expect(await screen.findByText('Message history unavailable (503).')).toBeInTheDocument()
    expect(container.querySelectorAll('.gm-thread__exchange')).toHaveLength(HYDRATE_TARGET)
    await waitFor(() => expect(announcer).toHaveTextContent('Couldn’t load earlier turns'))

    // Same control, same kept cursor — a retry, not a dead end (STATE-2).
    await userEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    await waitFor(() => expect(container.querySelectorAll('.gm-thread__exchange')).toHaveLength(HYDRATE_TARGET + 1))
    expect(screen.queryByText('Message history unavailable (503).')).toBeNull()
    expect(load.mock.calls.filter(([, cursor]) => cursor === 'p1')).toHaveLength(2)
  })

  it('keeps the reader’s scroll position when older turns are prepended above it', async () => {
    const load = pagedTimeline([manyChatEntries(HYDRATE_TARGET, 9300), [chatEntry({ entry_id: 'ent_older4', prompt: OLDER_PROMPT })]])
    const { container } = render(<Pane nav={{ conversationId: 'cnv_1' }} loadTimeline={load} />)
    await waitFor(() => expect(container.querySelectorAll('.gm-thread__exchange')).toHaveLength(HYDRATE_TARGET))

    const feed = container.querySelector('.chat-pane__exchanges')!
    // Scrolled up near the top, where Load earlier lives — not at the bottom.
    // Firing the scroll event matters: without it `atBottom` keeps its
    // fresh-thread default of true, and pp6q.1.3's own autoscroll effect
    // would jump the feed straight to the bottom on the very same update.
    const geo = stubGeometry(feed, { scrollHeight: 5000, clientHeight: 400, scrollTop: 30 })
    fireEvent.scroll(feed)

    fireEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    // The feed grows by 200px of content once the older turn renders — jsdom
    // performs no layout, so the test stands in for the browser here.
    geo.scrollHeight = 5200

    await waitFor(() => expect(screen.getByText(OLDER_PROMPT)).toBeInTheDocument())
    expect(geo.scrollTop).toBe(230)
  })

  it('never fires the settle announcement for a switched-to conversation once an old walk resolves late', async () => {
    let resolveOlderA: ((result: TimelinePageResult) => void) | null = null
    const loadA: LoadTimelinePageFn = async (conversationId, cursor) => {
      if (cursor === null) {
        return { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 9500), next_cursor: 'p1' } }
      }
      return new Promise((resolve) => {
        resolveOlderA = resolve
      })
    }
    const loadB = pagedTimeline([[chatEntry({ entry_id: 'ent_b', prompt: 'A question about cnv_b' })]])

    const { rerender } = render(<Pane nav={{ conversationId: 'cnv_a' }} loadTimeline={loadA} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load earlier' })).toBeInTheDocument())
    const announcer = screen.getByRole('status')

    await userEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    expect(announcer).toHaveTextContent('Loading earlier turns…')

    // Away to a different conversation before cnv_a's walk ever resolves.
    rerender(<Pane nav={{ conversationId: 'cnv_b' }} loadTimeline={loadB} />)
    await waitFor(() => expect(screen.getByText('A question about cnv_b')).toBeInTheDocument())
    // The bug this guards: `useGmTimeline` reads `loadingEarlier` as false
    // for a freshly-switched-to scope (its LOADING sentinel), which looks
    // exactly like cnv_a's walk just settling unless the switch itself resets
    // the settle-announcement's own tracking too.
    expect(announcer).not.toHaveTextContent('Earlier turns loaded')
    expect(announcer).not.toHaveTextContent('Couldn’t load earlier turns')

    await act(async () => {
      resolveOlderA?.({
        kind: 'ok',
        page: { conversation_id: 'cnv_a', items: [chatEntry({ entry_id: 'ent_older_a' })], next_cursor: null },
      })
    })
    // And cnv_a's now-irrelevant walk resolving late changes nothing either.
    expect(announcer).not.toHaveTextContent('Earlier turns loaded')
    expect(announcer).not.toHaveTextContent('Couldn’t load earlier turns')
  })
})
