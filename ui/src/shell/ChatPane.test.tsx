import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent, isInaccessible } from '@testing-library/react'
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
import { CHAT_TEXT_MAX_CHARS } from '../gm/contracts'
import type {
  Attachment,
  AttachmentsResult,
  ChatResult,
  MessagesResult,
  StoredMessage,
  TimelinePageResult,
  UploadAttachmentResult,
} from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'
import type { LoadTimelinePageFn } from '../gm/gmTimeline'
import type { GetAttachmentsFn, UploadAttachmentFn } from './ChatPane'
import { ModelPicker, type GetModelsFn } from './ModelPicker'
import { ModelCatalogProvider } from './ModelCatalogContext'

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
  loadTimeline,
  uploadAttachment,
  getAttachments,
  store = new MemoryConversationStore(),
}: {
  navState?: Partial<AppNavState>
  post?: PostFn
  loadHistory?: LoadHistoryFn
  loadTimeline?: LoadTimelinePageFn
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
              loadTimeline={loadTimeline}
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

describe('ChatPane — reading column + parchment (pp6q.1.2)', () => {
  // jsdom applies no stylesheet layout, so these assert the STRUCTURAL
  // contract the CSS hangs off — that the ground class is applied and that a
  // dedicated column element wraps the messages. The visual judgment (how wide
  // the column should be) is made against the live demo, not here.

  it('applies the design system parchment ground to the feed', () => {
    const { container } = render(<Wrapper />)
    expect(container.querySelector('.chat-pane__exchanges')?.classList)
      .toContain('aether-parchment')
  })

  it('wraps messages in a reading column rather than letting them span the feed', async () => {
    const post: PostFn = async () => ({
      kind: 'ok',
      response: { answer: 'A basilisk petrifies.', sources: [], answerable: true },
    })
    const { container } = render(<Wrapper post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'basilisk?')
    await userEvent.keyboard('{Enter}')

    const column = container.querySelector('.chat-pane__column')
    expect(column).not.toBeNull()
    await waitFor(() =>
      expect(column!.textContent).toContain('A basilisk petrifies.'),
    )
  })
})

describe('ChatPane — autoscroll + jump-to-latest (pp6q.1.3)', () => {
  // jsdom performs NO layout: scrollTop/scrollHeight/clientHeight are all 0
  // unless defined. Without this helper these tests would pass vacuously —
  // "scrolled to the bottom" is trivially true when 0 === 0 - 0.
  function stubGeometry(
    el: Element, { scrollHeight, clientHeight, scrollTop }:
    { scrollHeight: number; clientHeight: number; scrollTop: number },
  ) {
    Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
    Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
    let top = scrollTop
    Object.defineProperty(el, 'scrollTop', {
      get: () => top,
      set: (v: number) => { top = v },
      configurable: true,
    })
    return { get scrollTop() { return top }, set scrollTop(v: number) { top = v } }
  }

  const answer = (text: string): PostFn => async () => ({
    kind: 'ok',
    response: { answer: text, sources: [], answerable: true },
  })

  async function send(text: string) {
    await userEvent.type(screen.getByPlaceholderText('Ask…'), text)
    await userEvent.keyboard('{Enter}')
  }

  it('scrolls to the newest message when the user is already at the bottom', async () => {
    const { container } = render(<Wrapper post={answer('Reply one.')} />)
    const feed = container.querySelector('.chat-pane__exchanges')!
    // At the bottom: scrollTop === scrollHeight - clientHeight.
    const geo = stubGeometry(feed, { scrollHeight: 1000, clientHeight: 400, scrollTop: 600 })

    await send('q1')
    await waitFor(() => expect(screen.getByText('Reply one.')).toBeInTheDocument())
    await waitFor(() => expect(geo.scrollTop).toBe(1000))
  })

  it('does NOT scroll when the user has scrolled up to read', async () => {
    const { container } = render(<Wrapper post={answer('Reply two.')} />)
    const feed = container.querySelector('.chat-pane__exchanges')!
    // Far from the bottom — the user is reading earlier history.
    const geo = stubGeometry(feed, { scrollHeight: 1000, clientHeight: 400, scrollTop: 50 })
    fireEvent.scroll(feed)

    await send('q2')
    await waitFor(() => expect(screen.getByText('Reply two.')).toBeInTheDocument())
    expect(geo.scrollTop).toBe(50)
  })

  it('hides the jump-to-latest control while at the bottom', async () => {
    const { container } = render(<Wrapper post={answer('Reply.')} />)
    const feed = container.querySelector('.chat-pane__exchanges')!
    stubGeometry(feed, { scrollHeight: 1000, clientHeight: 400, scrollTop: 600 })
    // A populated thread — otherwise this passes vacuously on the
    // empty-thread guard rather than on the at-bottom check it names.
    await send('q')
    await waitFor(() => expect(screen.getByText('Reply.')).toBeInTheDocument())
    fireEvent.scroll(feed)
    expect(screen.queryByRole('button', { name: /jump to latest/i })).toBeNull()
  })

  it('never offers jump-to-latest on an empty thread, however it is scrolled', async () => {
    const { container } = render(<Wrapper post={answer('x')} />)
    const feed = container.querySelector('.chat-pane__exchanges')!
    stubGeometry(feed, { scrollHeight: 1000, clientHeight: 400, scrollTop: 50 })
    fireEvent.scroll(feed)
    expect(screen.queryByRole('button', { name: /jump to latest/i })).toBeNull()
  })

  it('shows jump-to-latest once the user scrolls away, and returns to the bottom when used', async () => {
    const { container } = render(<Wrapper post={answer('Reply.')} />)
    const feed = container.querySelector('.chat-pane__exchanges')!
    const geo = stubGeometry(feed, { scrollHeight: 1000, clientHeight: 400, scrollTop: 600 })
    await send('q')
    await waitFor(() => expect(screen.getByText('Reply.')).toBeInTheDocument())

    // Now the reader scrolls up to re-read.
    geo.scrollTop = 50
    fireEvent.scroll(feed)

    const jump = await screen.findByRole('button', { name: /jump to latest/i })
    await userEvent.click(jump)
    expect(geo.scrollTop).toBe(1000)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /jump to latest/i })).toBeNull(),
    )
  })

  it('treats a sub-pixel gap from the bottom as still "at the bottom"', async () => {
    // Fractional scroll offsets are routine under browser zoom and HiDPI; an
    // exact scrollTop === scrollHeight - clientHeight test would call this
    // "scrolled away" and stop following for a user who never moved.
    const { container } = render(<Wrapper post={answer('Reply three.')} />)
    const feed = container.querySelector('.chat-pane__exchanges')!
    const geo = stubGeometry(feed, { scrollHeight: 1000, clientHeight: 400, scrollTop: 598.5 })
    fireEvent.scroll(feed)

    await send('q3')
    await waitFor(() => expect(screen.getByText('Reply three.')).toBeInTheDocument())
    await waitFor(() => expect(geo.scrollTop).toBe(1000))
    expect(screen.queryByRole('button', { name: /jump to latest/i })).toBeNull()
  })
})

describe('ChatPane — composer (pp6q.1.4)', () => {
  // The composer opts into autoGrow, which changes how the textarea is sized.
  // These pin the send semantics that sizing must not disturb.

  it('still sends on Enter', async () => {
    const sent: string[] = []
    const post: PostFn = async (prompt) => { sent.push(prompt); return GROUNDED }
    render(<Wrapper post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'a question')
    await userEvent.keyboard('{Enter}')
    expect(sent).toEqual(['a question'])
  })

  it('still inserts a newline on Shift+Enter without sending', async () => {
    const post = vi.fn<PostFn>(async () => GROUNDED)
    render(<Wrapper post={post} />)
    const ta = screen.getByPlaceholderText('Ask…') as HTMLTextAreaElement
    await userEvent.type(ta, 'line one')
    await userEvent.keyboard('{Shift>}{Enter}{/Shift}')
    await userEvent.type(ta, 'line two')
    expect(post).not.toHaveBeenCalled()
    expect(ta.value).toBe('line one\nline two')
  })

  it('agent-forge-harness-764: disables Send and shows a counter past CHAT_TEXT_MAX_CHARS', async () => {
    const post = vi.fn<PostFn>(async () => GROUNDED)
    render(<Wrapper post={post} />)
    const ta = screen.getByPlaceholderText('Ask…') as HTMLTextAreaElement
    // fireEvent.change, not userEvent.type: this draft is 100,001 characters —
    // typing it key by key would be a real per-character simulation, not a
    // meaningfully different test.
    fireEvent.change(ta, { target: { value: 'a'.repeat(CHAT_TEXT_MAX_CHARS + 1) } })
    expect(screen.getByText(`${CHAT_TEXT_MAX_CHARS + 1} of ${CHAT_TEXT_MAX_CHARS} characters`, { exact: false })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    // fireEvent.change doesn't focus the textarea; without the click, {Enter}
    // would land on document.body and never reach the composer's key handler.
    await userEvent.click(ta)
    expect(ta).toHaveFocus()
    await userEvent.keyboard('{Enter}')
    expect(post).not.toHaveBeenCalled()
    // A send would also clear the draft: the user's over-long text must survive.
    expect(ta.value.length).toBe(CHAT_TEXT_MAX_CHARS + 1)
  })

  it('agent-forge-harness-764 × 4oz: the over-length counter describes the field and adds no second live region', () => {
    const { container } = render(<Wrapper />)
    // The pane's one announcer, captured at rest (the 4oz tests' own shape).
    const [announcer] = screen.getAllByRole('status')
    const ta = screen.getByPlaceholderText('Ask…') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'a'.repeat(CHAT_TEXT_MAX_CHARS + 1) } })
    // 764: the refusal is still said, and said ON the field it is about.
    expect(ta).toHaveAttribute('aria-invalid', 'true')
    expect(ta).toHaveAccessibleDescription(
      `${CHAT_TEXT_MAX_CHARS + 1} of ${CHAT_TEXT_MAX_CHARS} characters — shorten your message to send it.`,
    )
    // 4oz: not by a second live region beside the announcer — counted in every
    // live-region shape, not role="status" alone — and not by the announcer,
    // whose text changes only when a turn is sent or settles.
    const live = container.querySelectorAll(
      '[role="status"], [role="alert"], [role="log"], [aria-live]:not([aria-live="off"])',
    )
    expect(Array.from(live)).toEqual([announcer])
    expect(announcer.textContent).toBe('')
  })
})

describe('ChatPane — one "Attach file" control (agent-forge-harness-vnx)', () => {
  // Two controls in the composer used to share the accessible name "Attach
  // file": the hidden <input type="file"> and the visible IconButton that
  // clicks it. A screen-reader user tabbing the composer met two named
  // controls, one of which does nothing on its own.

  it('the hidden file input is out of the accessibility tree and out of the tab order (V1)', () => {
    render(<Wrapper />)
    const input = screen.getByLabelText('Attach file', { selector: 'input' })
    expect(input).toHaveAttribute('aria-hidden', 'true')
    expect(input.tabIndex).toBe(-1)
    // isInaccessible is imported from @testing-library/react (never
    // @testing-library/dom, which is not a declared dependency); it checks
    // `aria-hidden`/`hidden`/`display: none`, not off-screen clipping, so it
    // is false before this fix and true after.
    expect(isInaccessible(input)).toBe(true)
  })

  it('exactly one element in the accessibility tree is named "Attach file"', () => {
    render(<Wrapper />)
    // Testing Library gives a file input no role, so this counts only the
    // real affordance — the visible IconButton.
    expect(screen.getAllByRole('button', { name: 'Attach file' })).toHaveLength(1)
  })
})

describe('ChatPane — typing indicator (pp6q.1.5)', () => {
  function pendingForever(): PostFn {
    return () => new Promise<ChatResult>(() => {})
  }

  it('shows the animated dot row while a reply is pending', async () => {
    const { container } = render(<Wrapper post={pendingForever()} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'q')
    await userEvent.keyboard('{Enter}')
    await waitFor(() =>
      expect(container.querySelectorAll('.chat-pane__dot')).toHaveLength(3),
    )
  })

  it('still announces the pending state to assistive tech (agent-forge-harness-4oz: on the single already-mounted live region, not a freshly-mounted node)', async () => {
    // The dots are decoration. Replacing the announcement with a purely
    // visual animation would be an accessibility regression dressed as
    // polish — a screen-reader user would get no signal that anything is
    // happening at all.
    //
    // Capture the pane's one live region BEFORE the send below — while it is
    // still empty, at first render — and assert the mutation on that SAME
    // reference. Asserting instead on whatever `role="status"` node happens
    // to exist right after sending would be asserting on a node the test's
    // own action just created (with its text already inside it), which is
    // exactly the shape that fails to announce for real assistive tech and
    // the reason this bead exists.
    render(<Wrapper post={pendingForever()} />)
    const [announcer] = screen.getAllByRole('status')
    expect(announcer.textContent).toBe('')

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'q')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(announcer.textContent?.trim()).not.toBe(''))
  })

  // agent-forge-harness-swg (pr116 M-2): counting only `role="status"`
  // let a live region survive uncounted in any OTHER form — an `aria-live`
  // span, or `role="alert"`/`role="log"` — so the census below matches every
  // way a node can be a live region, not just the one shape this pane
  // currently happens to use. Sampled at rest, pending, settled AND while
  // history recalls (pr116 M-1's carry item — the old test never sampled
  // recall, which is exactly where the second live region was hiding).
  function liveRegions(container: HTMLElement): NodeListOf<Element> {
    return container.querySelectorAll(
      '[role="status"],[role="alert"],[role="log"],[aria-live]',
    )
  }

  it('agent-forge-harness-4oz / agent-forge-harness-swg: exactly one live region exists in the pane at every state — recall, rest, pending and settled', async () => {
    let resolveHistory!: (r: MessagesResult) => void
    const loadHistory: LoadHistoryFn = () =>
      new Promise<MessagesResult>((res) => {
        resolveHistory = res
      })
    let resolvePost!: (r: ChatResult) => void
    const post: PostFn = () => new Promise<ChatResult>((res) => {
      resolvePost = res
    })

    const { container } = render(
      <Wrapper navState={{ conversationId: 'conv-1' }} post={post} loadHistory={loadHistory} />,
    )

    // Recall.
    expect(liveRegions(container)).toHaveLength(1)

    act(() => resolveHistory({ kind: 'ok', messages: [] }))
    await waitFor(() => expect(screen.getByText('Ask the Sage…')).toBeInTheDocument())

    // Rest.
    expect(liveRegions(container)).toHaveLength(1)

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'q')
    await userEvent.keyboard('{Enter}')

    // Pending.
    await waitFor(() => expect(liveRegions(container)).toHaveLength(1))

    act(() => resolvePost(GROUNDED))
    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )

    // Settled.
    expect(liveRegions(container)).toHaveLength(1)
  })

  // agent-forge-harness-swg (pr129 M-3): the census above never sampled an
  // ERROR state or the GM side — and `role="alert"`, the one live-region form
  // nothing else here checks, is exactly what an error state would reach for.
  // Each state below is sampled only once its own text is on screen.

  it('agent-forge-harness-swg: exactly one live region while a history-recall error is shown', async () => {
    const loadHistory: LoadHistoryFn = async () => ({ kind: 'error', message: 'History is unavailable.' })
    const { container } = render(
      <Wrapper navState={{ conversationId: 'conv-1' }} loadHistory={loadHistory} />,
    )
    await screen.findByText('History is unavailable.')
    expect(liveRegions(container)).toHaveLength(1)
  })

  it('agent-forge-harness-swg: exactly one live region while a failed turn is shown', async () => {
    const post: PostFn = async () => ({ kind: 'error', message: 'The service is busy.' })
    const { container } = render(<Wrapper post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'q')
    await userEvent.keyboard('{Enter}')
    await screen.findByText('The service is busy.')
    expect(liveRegions(container)).toHaveLength(1)
  })

  it('agent-forge-harness-swg: exactly one live region on the GM side — timeline recall and timeline error', async () => {
    let resolveTimeline!: (r: TimelinePageResult) => void
    const loadTimeline: LoadTimelinePageFn = () =>
      new Promise<TimelinePageResult>((res) => {
        resolveTimeline = res
      })
    const { container } = render(
      <Wrapper navState={{ mode: 'gm', conversationId: 'conv-g' }} loadTimeline={loadTimeline} />,
    )

    // Timeline recall.
    await screen.findByText('Recalling the conversation…')
    expect(liveRegions(container)).toHaveLength(1)

    // Timeline error.
    act(() => resolveTimeline({ kind: 'error', message: 'Timeline is unavailable.' }))
    await screen.findByText('Timeline is unavailable.')
    expect(liveRegions(container)).toHaveLength(1)
  })

  it('agent-forge-harness-swg: exactly one live region on the GM side — rest, pending, settled and a failed turn', async () => {
    const resolvers: Array<(r: ChatResult) => void> = []
    const post: PostFn = () => new Promise<ChatResult>((res) => { resolvers.push(res) })
    const { container } = render(<Wrapper navState={{ mode: 'gm' }} post={post} />)

    // Rest.
    expect(liveRegions(container)).toHaveLength(1)

    // Pending.
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'first')
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(resolvers).toHaveLength(1))
    expect(liveRegions(container)).toHaveLength(1)

    // Settled.
    act(() => resolvers[0](GROUNDED))
    await screen.findByText('A basilisk petrifies with its gaze.')
    expect(liveRegions(container)).toHaveLength(1)

    // A failed turn.
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'second')
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(resolvers).toHaveLength(2))
    act(() => resolvers[1]({ kind: 'error', message: 'The GM service is busy.' }))
    await screen.findByText('The GM service is busy.')
    expect(liveRegions(container)).toHaveLength(1)
  })

  it('hides the dots once the reply arrives', async () => {
    const post: PostFn = async () => GROUNDED
    const { container } = render(<Wrapper post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'q')
    await userEvent.keyboard('{Enter}')
    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )
    expect(container.querySelectorAll('.chat-pane__dot')).toHaveLength(0)
  })
})

function storedTurn(id: number, prompt: string, reply: string): StoredMessage[] {
  return [
    { id, role: 'user', content: prompt, mode: 'sage', created_at: '2026-09-18T19:02:00Z' },
    { id: id + 1, role: 'assistant', content: reply, mode: 'sage', created_at: '2026-09-18T19:02:04Z' },
  ]
}

function StatefulNavWrapper({
  loadHistory,
  post,
  initialConversationId = 'conv-a',
}: {
  loadHistory: LoadHistoryFn
  post?: PostFn
  /** `null` opens a brand-new chat, whose first turn adopts the server-minted id (x5bz.3.2). */
  initialConversationId?: string | null
}): React.JSX.Element {
  const [conversationId, setConversationId] = React.useState<string | null>(initialConversationId)
  // A fast stub — without it, ChatPane's default falls through to a real
  // fetch, which jsdom does not short-circuit and which slows (sometimes
  // flakily) every test built on this wrapper.
  const getAttachments: GetAttachmentsFn = async () => ({ kind: 'ok', attachments: [] })
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={{ ...makeNavState(), conversationId, setConversationId }}>
        <CurrentUserContext.Provider value={makeUserState()}>
          <ConversationStoreProvider store={new MemoryConversationStore()}>
            <ChatPane post={post} loadHistory={loadHistory} getAttachments={getAttachments} />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
      <button type="button" onClick={() => setConversationId('conv-b')}>
        switch to B (test only)
      </button>
      <button type="button" onClick={() => setConversationId('conv-a')}>
        switch to A (test only)
      </button>
      <span data-testid="nav-conversation-id">{String(conversationId)}</span>
    </ThemeProvider>
  )
}

describe('ChatPane — arrival announcer (agent-forge-harness-ekf)', () => {
  function arrivalOf(container: HTMLElement): string | undefined {
    return container.querySelector('.chat-pane__arrival')?.textContent
  }

  it('E1a: exists as a status node outside the transcript, empty at first render with an empty thread', () => {
    const { container } = render(<Wrapper />)
    const announcer = container.querySelector('.chat-pane__arrival')
    expect(announcer).not.toBeNull()
    expect(announcer).toHaveAttribute('role', 'status')
    const transcript = screen.getByRole('region', { name: 'Conversation' })
    expect(transcript.contains(announcer)).toBe(false)
    expect(announcer?.textContent).toBe('')
  })

  it('E1c: stays empty after a history recall, with the recalled turn on screen', async () => {
    const loadHistory: LoadHistoryFn = async () => ({
      kind: 'ok',
      messages: storedTurn(1, 'Recalled question', 'Recalled answer'),
    })
    const { container } = render(
      <Wrapper navState={{ conversationId: 'conv-1' }} loadHistory={loadHistory} />,
    )
    await screen.findByText('Recalled question')
    expect(arrivalOf(container)).toBe('')
  })

  it('E2/E3/E5: announces "Answer received" once per turn, a fixed phrase, and re-announces pending on the next send (agent-forge-harness-4oz: the pending phrase replaces the old clear-to-empty)', async () => {
    const resolvers: Array<(r: ChatResult) => void> = []
    const post: PostFn = () => new Promise((res) => { resolvers.push(res) })
    const { container } = render(<Wrapper post={post} />)

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'first question')
    await userEvent.keyboard('{Enter}')
    // agent-forge-harness-4oz folded the pending announcement into this same
    // node: sending announces it immediately, rather than clearing to ''.
    expect(arrivalOf(container)).toBe('Consulting the tomes…')

    act(() => resolvers[0](GROUNDED))
    await waitFor(() => expect(arrivalOf(container)).toBe('Answer received'))
    // E3 — a fixed phrase, never the answer's own text (which appears once,
    // in the transcript, not twice).
    expect(screen.getAllByText('A basilisk petrifies with its gaze.')).toHaveLength(1)

    // E5 — a second arrival is a second announcement: the pending phrase is
    // re-announced on the SAME node for the second turn, not left standing
    // from the first turn's "Answer received".
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'second question')
    await userEvent.keyboard('{Enter}')
    expect(arrivalOf(container)).toBe('Consulting the tomes…')

    act(() => resolvers[1](GROUNDED))
    await waitFor(() => expect(arrivalOf(container)).toBe('Answer received'))
  })

  it('E4: announces "Answer failed" once, without repeating the error text, on a failed result', async () => {
    const post: PostFn = async () => ({
      kind: 'error',
      message: 'The service is busy right now — try again in a moment.',
    })
    const { container } = render(<Wrapper post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'q')
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(arrivalOf(container)).toBe('Answer failed'))
    expect(
      screen.getAllByText('The service is busy right now — try again in a moment.'),
    ).toHaveLength(1)
  })

  it('E4: announces "Answer failed" when post() rejects', async () => {
    const post: PostFn = () => Promise.reject(new Error('network snapped'))
    const { container } = render(<Wrapper post={post} />)
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'q')
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(arrivalOf(container)).toBe('Answer failed'))
  })

  it('E6: a conversation switch and the recall that follows announce nothing', async () => {
    const loadHistory: LoadHistoryFn = async (conversationId) =>
      conversationId === 'conv-a'
        ? { kind: 'ok', messages: storedTurn(1, 'Question A', 'Answer A') }
        : { kind: 'ok', messages: storedTurn(1, 'Question B', 'Answer B') }

    const { container } = render(<StatefulNavWrapper loadHistory={loadHistory} />)
    await screen.findByText('Question A')
    expect(arrivalOf(container)).toBe('')

    await userEvent.click(screen.getByRole('button', { name: /switch to b/i }))
    // Anchor on B's recalled turn being on screen before asserting the
    // absence (§8.5) — an absence asserted before the thing that could
    // break it has happened cannot fail.
    await screen.findByText('Question B')
    expect(arrivalOf(container)).toBe('')
  })

  it('agent-forge-harness-swg (pr114 M-1): a stale turn settling after the user left its conversation does not announce', async () => {
    // ChatPane is never remounted on a conversation switch (see the comment
    // on the transcript region), so its `arrival` node is SHARED across
    // conversations. `onTurnSettled` used to fire unconditionally, so a turn
    // sent from A that settled after the user switched to B would announce
    // "Answer received"/"Answer failed" into the pane B is showing, for a
    // turn B never displayed.
    //
    // A generous timeout: this scenario drives two conversations' worth of
    // effects (recall x2, attachments x2, a real send+settle) through
    // userEvent, which is consistently slower than this file's other tests
    // in CI-like sandboxes — confirmed finite (not hung) at ~6s.
    let resolvePost!: (r: ChatResult) => void
    const post: PostFn = () => new Promise<ChatResult>((res) => { resolvePost = res })
    const loadHistory: LoadHistoryFn = async (conversationId) =>
      conversationId === 'conv-a'
        ? { kind: 'ok', messages: [] }
        : { kind: 'ok', messages: storedTurn(1, 'Question B', 'Answer B') }

    const { container } = render(<StatefulNavWrapper post={post} loadHistory={loadHistory} />)
    await waitFor(() => expect(screen.getByPlaceholderText('Ask…')).toBeInTheDocument())

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'About goblins')
    await userEvent.keyboard('{Enter}')
    expect(arrivalOf(container)).toBe('Consulting the tomes…')

    // Switch to B before A's turn settles. A switch alone never changes
    // `arrival` (agent-forge-harness-ekf/4oz) — it is still announcing A's
    // now-abandoned pending turn.
    await userEvent.click(screen.getByRole('button', { name: /switch to b/i }))
    await screen.findByText('Question B')
    const beforeStaleSettle = arrivalOf(container)
    expect(beforeStaleSettle).toBe('Consulting the tomes…')

    // NOW the stale A turn settles. Without the fix this becomes "Answer
    // received" — B's pane announcing a turn B never showed.
    await act(async () => {
      resolvePost(GROUNDED)
    })
    expect(arrivalOf(container)).not.toBe('Answer received')
    expect(screen.queryByText('A basilisk petrifies with its gaze.')).toBeNull()
    // pr129 M-2: nor may it leave A's pending phrase standing — B's next
    // send would set the same text and go unannounced. It is quietly cleared.
    expect(arrivalOf(container)).toBe('')
  }, 15000)

  it('agent-forge-harness-swg (pr129 M-1): A -> B -> A before the turn settles announces nothing — the answer is never drawn', async () => {
    // Comparing conversation ids is not enough: the user is back on A, so
    // "sent for A" matches "on screen now" — but A's recall has replaced the
    // exchange list since, so the settle has nothing to write into and the
    // answer never appears. Only a settle that is actually APPLIED to the
    // exchanges on screen may announce.
    let resolvePost!: (r: ChatResult) => void
    const post: PostFn = () => new Promise<ChatResult>((res) => { resolvePost = res })
    const loadHistory: LoadHistoryFn = async (conversationId) =>
      conversationId === 'conv-a'
        ? { kind: 'ok', messages: storedTurn(1, 'Question A', 'Answer A') }
        : { kind: 'ok', messages: storedTurn(3, 'Question B', 'Answer B') }

    const { container } = render(<StatefulNavWrapper post={post} loadHistory={loadHistory} />)
    await screen.findByText('Question A')

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'About goblins')
    await userEvent.keyboard('{Enter}')
    expect(arrivalOf(container)).toBe('Consulting the tomes…')

    await userEvent.click(screen.getByRole('button', { name: /switch to b/i }))
    await screen.findByText('Question B')
    await userEvent.click(screen.getByRole('button', { name: /switch to a/i }))
    await screen.findByText('Question A')

    await act(async () => {
      resolvePost(GROUNDED)
    })

    // Anchor: the settle has happened and drew nothing on A's screen.
    expect(screen.queryByText('A basilisk petrifies with its gaze.')).toBeNull()
    expect(arrivalOf(container)).not.toBe('Answer received')
    // And the pending phrase it was left holding is quietly cleared (M-2).
    expect(arrivalOf(container)).toBe('')
  }, 15000)

  it('agent-forge-harness-swg (pr129 M-2): after a suppressed stale settle, the next send in B still changes the announcer', async () => {
    // A suppressed settle used to leave A's "Consulting the tomes…" standing
    // in the shared announcer. B's next send then set the SAME text, so the
    // live region's DOM never changed and B's pending state was never
    // announced (pp6q.1.5 / agent-forge-harness-4oz).
    const resolvers: Array<(r: ChatResult) => void> = []
    const post: PostFn = () => new Promise<ChatResult>((res) => { resolvers.push(res) })
    const loadHistory: LoadHistoryFn = async (conversationId) =>
      conversationId === 'conv-a'
        ? { kind: 'ok', messages: [] }
        : { kind: 'ok', messages: storedTurn(1, 'Question B', 'Answer B') }

    const { container } = render(<StatefulNavWrapper post={post} loadHistory={loadHistory} />)
    await waitFor(() => expect(screen.getByPlaceholderText('Ask…')).toBeInTheDocument())

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'About goblins')
    await userEvent.keyboard('{Enter}')
    await userEvent.click(screen.getByRole('button', { name: /switch to b/i }))
    await screen.findByText('Question B')
    await act(async () => {
      resolvers[0](GROUNDED)
    })

    // Watch the live region itself: a screen reader announces a CHANGE to it.
    const announcer = container.querySelector('.chat-pane__arrival') as HTMLElement
    const mutations: MutationRecord[] = []
    const observer = new MutationObserver((records) => mutations.push(...records))
    observer.observe(announcer, { childList: true, characterData: true, subtree: true })
    try {
      await userEvent.type(screen.getByPlaceholderText('Ask…'), 'About B')
      await userEvent.keyboard('{Enter}')
      await waitFor(() => expect(resolvers).toHaveLength(2))
      // MutationObserver callbacks are microtasks — let them drain.
      await act(async () => {
        await Promise.resolve()
      })
      expect(mutations.length).toBeGreaterThan(0)
      expect(arrivalOf(container)).toBe('Consulting the tomes…')
    } finally {
      observer.disconnect()
    }

    // B's own turn, drawn on B's screen, still announces its arrival.
    await act(async () => {
      resolvers[1](GROUNDED)
    })
    await screen.findByText('A basilisk petrifies with its gaze.')
    expect(arrivalOf(container)).toBe('Answer received')
  }, 15000)

  it('agent-forge-harness-swg (pr129 M-4): the first turn of a NEW chat announces "Answer received" after the pane adopts the minted id', async () => {
    // A new chat sends with a null id; the server mints one and the pane
    // adopts it (x5bz.3.2) in the same tick the turn settles. That is the
    // SAME conversation, not a switch away — its first answer must announce.
    const post: PostFn = async () => ({
      kind: 'ok',
      response: {
        ...(GROUNDED.kind === 'ok' ? GROUNDED.response : ({} as never)),
        conversation_id: 'minted-1',
      },
    })
    const loadHistory: LoadHistoryFn = async () => ({
      kind: 'ok',
      messages: storedTurn(1, 'About dragons', 'A basilisk petrifies with its gaze.'),
    })

    const { container } = render(
      <StatefulNavWrapper post={post} loadHistory={loadHistory} initialConversationId={null} />,
    )
    expect(screen.getByTestId('nav-conversation-id')).toHaveTextContent('null')

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'About dragons')
    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByTestId('nav-conversation-id')).toHaveTextContent('minted-1'))
    await screen.findByText('A basilisk petrifies with its gaze.')
    await waitFor(() => expect(arrivalOf(container)).toBe('Answer received'))
  }, 15000)
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

    // Resolve the post so the test can clean up, and wait for the settle to
    // land (the announcer's own text is asserted by the "arrival announcer"
    // describe block below — not this test's concern).
    act(() => resolvePost(GROUNDED))
    await waitFor(() =>
      expect(screen.getByText('A basilisk petrifies with its gaze.')).toBeInTheDocument(),
    )
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

  it('shows a pending status while waiting for a response (agent-forge-harness-4oz: the single pane-wide live region)', async () => {
    let resolvePost!: (r: ChatResult) => void
    const post: PostFn = () =>
      new Promise<ChatResult>((res) => {
        resolvePost = res
      })

    render(<Wrapper post={post} />)

    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'Q')
    await userEvent.keyboard('{Enter}')

    // Exactly one live region for the whole pane — captured here so the
    // settle assertion below is on the SAME node, not a new one.
    const [announcer] = screen.getAllByRole('status')
    expect(announcer).toHaveTextContent(/consulting the tomes/i)

    act(() => resolvePost(GROUNDED))
    await waitFor(() => expect(announcer).toHaveTextContent('Answer received'))
    expect(screen.getAllByRole('status')).toHaveLength(1)
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
    // Sage, the other channel that answers with a stat block: its card is the
    // full-density one this test pins. In the GM channel the card sits in the
    // assistant lane at compact density (1kg.3.4) — ChatPaneGm.test.tsx and
    // gm/GmThread.test.tsx pin that.
    render(<Wrapper navState={{ mode: 'sage' }} post={post} />)

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

describe('ChatPane — model preference wiring (agent-forge-harness-bta)', () => {
  // b8o.2's AC ("ChatRequest sends an allowlisted model_preference") is unmet
  // in the shipped UI unless the preference the picker shows actually reaches
  // `post`. Before bta, ChatPane called useChat with no `modelPreference` at
  // all, so useChat's own default ('auto') went out on every turn — which is
  // also why every UI conversation first sent before bta is bound 'auto'
  // server-side, whatever it stored. These tests mount the REAL picker and
  // pane on one store, the way WorkspaceShell does, so "what the picker
  // shows is what the pane sends" is proven about the two together.
  const emptyHistory: LoadHistoryFn = async () => ({ kind: 'ok', messages: [] })
  const noAttachments: GetAttachmentsFn = async () => ({ kind: 'ok', attachments: [] })
  const CATALOG_AFTER_D9 = {
    default: 'auto',
    models: [
      { id: 'auto', display_name: 'Automatic' },
      { id: 'traveller', display_name: 'Traveller', tier: 'traveller', supports_attachments: true },
    ],
  }
  const catalogDown: GetModelsFn = async () => { throw new Error('network error') }

  function Workspace({ store, conversationId, post, getModels }: {
    store: MemoryConversationStore
    conversationId: string
    post: PostFn
    getModels: GetModelsFn
  }): React.JSX.Element {
    return (
      <ThemeProvider>
        <AppNavContext.Provider value={makeNavState({ conversationId })}>
          <CurrentUserContext.Provider value={makeUserState()}>
            <ConversationStoreProvider store={store}>
              <ModelCatalogProvider>
                <ModelPicker getModels={getModels} confirmChange={() => true} />
                <ChatPane post={post} loadHistory={emptyHistory} getAttachments={noAttachments} />
              </ModelCatalogProvider>
            </ConversationStoreProvider>
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>
    )
  }

  const picker = () => screen.getByRole('combobox', { name: 'Model' }) as HTMLSelectElement
  const catalogServed = () => waitFor(() => expect(screen.getByRole('option', { name: 'Traveller' })).toBeInTheDocument())
  async function ask(prompt: string): Promise<void> {
    await userEvent.type(screen.getByPlaceholderText('Ask…'), prompt)
    await userEvent.keyboard('{Enter}')
  }
  const sent = (post: ReturnType<typeof vi.fn<PostFn>>) => post.mock.calls.map((call) => call[3])

  it("sends the active conversation's stored modelPreference, not useChat's default", async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', undefined, 'traveller')
    const post = vi.fn<PostFn>(async () => GROUNDED)
    render(<Workspace store={store} conversationId={conv.id} post={post} getModels={async () => CATALOG_AFTER_D9} />)
    await catalogServed()

    await ask('What is a basilisk?')

    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('What is a basilisk?', 'sage', conv.id, 'traveller')
  })

  it('sends a model picked through the picker AFTER mount, then keeps sending it once bound', async () => {
    // M2 of the review: seeding the store before mount cannot tell a live read
    // of the store from a stale one; a pick made after mount can.
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    const post = vi.fn<PostFn>(async () => GROUNDED)
    render(<Workspace store={store} conversationId={conv.id} post={post} getModels={async () => CATALOG_AFTER_D9} />)
    await catalogServed()

    await userEvent.selectOptions(picker(), 'traveller')
    await ask('What is a basilisk?')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    await ask('And a cockatrice?')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))

    expect(sent(post)).toEqual(['traveller', 'traveller'])
    expect(store.get(conv.id)?.boundPreference).toBe('traveller')
    expect(picker().value).toBe('traveller')
  })

  it("falls back to 'auto' for a conversation with no stored preference", async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    const post = vi.fn<PostFn>(async () => GROUNDED)
    render(<Workspace store={store} conversationId={conv.id} post={post} getModels={async () => CATALOG_AFTER_D9} />)
    await catalogServed()

    await ask('What is a basilisk?')

    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('What is a basilisk?', 'sage', conv.id, 'auto')
  })

  it.each([
    ['served', true],
    ['down (so the a6o reset never runs)', false],
  ])("posts 'auto' for a conversation first sent before bta, whatever alias it stored, catalog %s (B1)", async (_catalog, up) => {
    // Master's picker stored the model ALIAS and master's pane posted nothing,
    // so the server bound this conversation ('auto', None). /chat refuses the
    // alias on such a binding with a 422 — on every turn.
    const store = new MemoryConversationStore()
    const conv = store.create('sage', 'How does grappling work?', 'gpt-4o-mini')
    const post = vi.fn<PostFn>(async () => GROUNDED)
    const getModels = vi.fn<GetModelsFn>(up ? async () => CATALOG_AFTER_D9 : catalogDown)
    render(<Workspace store={store} conversationId={conv.id} post={post} getModels={getModels} />)
    if (up) await catalogServed()
    else await waitFor(() => expect(getModels).toHaveBeenCalled())
    await act(async () => {})

    await ask('And a shove?')

    await waitFor(() => expect(post).toHaveBeenCalled())
    expect({ posted: sent(post), shown: picker().value }).toEqual({ posted: ['auto'], shown: 'auto' })
  })

  it.each(['gpt-4o-mini', 'traveller'])(
    'with only the offline fallback catalog, posts what the picker shows — never the stored %s (B1)',
    async (stored) => {
      const store = new MemoryConversationStore()
      const conv = store.create('sage', undefined, stored)
      const post = vi.fn<PostFn>(async () => GROUNDED)
      const getModels = vi.fn(catalogDown)
      render(<Workspace store={store} conversationId={conv.id} post={post} getModels={getModels} />)
      await waitFor(() => expect(getModels).toHaveBeenCalled())
      await act(async () => {})

      await ask('What is a basilisk?')

      await waitFor(() => expect(post).toHaveBeenCalled())
      expect({ posted: sent(post), shown: picker().value }).toEqual({ posted: ['auto'], shown: 'auto' })
      expect(store.get(conv.id)?.boundPreference).toBe('auto')
    },
  )

  it('keeps posting the preference a conversation was bound with, even when the catalog is down', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', undefined, 'traveller')
    store.recordFirstPrompt(conv.id, 'What is a basilisk?', 'traveller')
    const post = vi.fn<PostFn>(async () => GROUNDED)
    render(<Workspace store={store} conversationId={conv.id} post={post} getModels={catalogDown} />)

    await ask('And a cockatrice?')

    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(sent(post)).toEqual(['traveller'])
  })
})
