/**
 * ChatPane in the GM channel, 1kg.3.5 PR-B: the composer while a turn is in
 * flight, and the thread's session dividers across Load earlier.
 *
 * RAIL-16 (I-14 as the critic amended it): in the GM channel — the pane on the
 * GM side AND the mode `gm` — the field is never disabled. Send and Enter wait
 * while this pane has a plain turn in flight in ANY conversation, and keep the
 * GM's text; a working lane never blocks (pending tool work is 1kg.4.5's to
 * wire). Sage, Spell and Rules, and both held states of a turn crossing the
 * GM boundary, keep today's lock (X-9; ChatPaneGm.test.tsx's own cases stay).
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
import type { ChatMode, ChatResult } from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'
import type { LoadTimelinePageFn } from '../gm/gmTimeline'
import { DIVIDER_COPY } from '../gm/gmTimeline'
import { QUIET_SESSION, chatEntry, manyChatEntries, pagedTimeline } from '../gm/threadFixtures'

function composerNav(overrides: Partial<AppNavState>): AppNavState {
  return {
    screen: 'workspace',
    mode: 'gm',
    conversationId: 'cnv_1',
    enterWorkspace: vi.fn(),
    setMode: vi.fn(),
    setConversationId: vi.fn(),
    backToLanding: vi.fn(),
    openProfile: vi.fn(),
    backToWorkspace: vi.fn(),
    ...overrides,
  }
}

const COMPOSER_USER: CurrentUserContextValue = {
  user: { id: 'gm-1', displayName: 'Game Master', initials: 'GM', role: 'dm', signOut: vi.fn(), editProfile: vi.fn() },
  authStatus: 'authenticated',
  retryAuthCheck: vi.fn(),
  signIn: vi.fn(),
  setDisplayName: vi.fn(),
  setAvatarTone: vi.fn(),
}

const composerStore = new MemoryConversationStore()
const noFiles: ChatPaneProps['getAttachments'] = async () => ({ kind: 'ok', attachments: [] })
const noHistory: LoadHistoryFn = async () => ({ kind: 'ok', messages: [] })
const noTimeline: LoadTimelinePageFn = async () => ({ kind: 'missing' })

function ComposerPane({ nav, ...props }: ChatPaneProps & { nav: Partial<AppNavState> }): React.JSX.Element {
  return (
    <ThemeProvider>
      <AppNavContext.Provider value={composerNav(nav)}>
        <CurrentUserContext.Provider value={COMPOSER_USER}>
          <ConversationStoreProvider store={composerStore}>
            <ChatPane getAttachments={noFiles} loadHistory={noHistory} loadTimeline={noTimeline} {...props} />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>
  )
}

const ANSWER: ChatResult = { kind: 'ok', response: { answer: 'The ferryman.', sources: [], answerable: true } }

/** A `post` whose every call waits until the test answers it. */
function heldService() {
  const calls: { args: [string, ChatMode, string | null]; resolve: (r: ChatResult) => void }[] = []
  const post: PostFn = (prompt, mode, conversationId) =>
    new Promise<ChatResult>((resolve) => {
      calls.push({ args: [prompt, mode, conversationId], resolve })
    })
  return { post, calls, posted: () => calls.map((call) => call.args) }
}

const field = (): HTMLTextAreaElement => screen.getByPlaceholderText('Ask…') as HTMLTextAreaElement
const sendButton = (): HTMLElement => screen.getByRole('button', { name: 'Send message' })

function liveRegionsIn(container: HTMLElement): Element[] {
  return [...container.querySelectorAll('[role="status"],[role="alert"],[role="log"],[aria-live]')]
}

describe('ChatPane (GM) — the composer while a turn is in flight (1kg.3.5, RAIL-16)', () => {
  it('the GM field stays live while a turn is in flight', async () => {
    const service = heldService()
    render(<ComposerPane nav={{}} post={service.post} />)
    await userEvent.type(field(), 'Who poles the ferry?')
    await userEvent.keyboard('{Enter}')
    expect(service.posted()).toEqual([['Who poles the ferry?', 'gm', 'cnv_1']])
    expect(await screen.findByText('Consulting the tomes…', { selector: '.assistant-lane__status-text' })).toBeInTheDocument()
    expect(field()).toBeEnabled()
    await userEvent.type(field(), 'And what does he charge?')
    expect(field()).toHaveValue('And what does he charge?')
  })

  it('Enter and Send wait for the turn in flight and keep the draft', async () => {
    const service = heldService()
    render(<ComposerPane nav={{}} post={service.post} />)
    await userEvent.type(field(), 'first')
    await userEvent.keyboard('{Enter}')
    await userEvent.type(field(), 'second')
    expect(sendButton()).toBeDisabled()
    await userEvent.keyboard('{Enter}')
    await userEvent.click(sendButton())
    expect(service.posted()).toEqual([['first', 'gm', 'cnv_1']])
    expect(field()).toHaveValue('second')

    await act(async () => service.calls[0].resolve(ANSWER))
    expect(await screen.findByText('The ferryman.')).toBeInTheDocument()
    expect(sendButton()).toBeEnabled()
    await userEvent.click(field())
    await userEvent.keyboard('{Enter}')
    expect(service.posted()).toEqual([
      ['first', 'gm', 'cnv_1'],
      ['second', 'gm', 'cnv_1'],
    ])
    expect(field()).toHaveValue('')
  })

  it('a turn in flight in one conversation keeps Send unavailable in another, and the text there', async () => {
    const service = heldService()
    const { rerender } = render(<ComposerPane nav={{ conversationId: 'cnv_1' }} post={service.post} />)
    await userEvent.type(field(), 'asked in the first thread')
    await userEvent.keyboard('{Enter}')

    rerender(<ComposerPane nav={{ conversationId: 'cnv_2' }} post={service.post} />)
    expect(await screen.findByText('Ask the Game Master…')).toBeInTheDocument()
    // Nothing is pending in cnv_2, but the pane still has a turn out: typing
    // is fine, sending would be refused by useChat and must not eat the text.
    expect(field()).toBeEnabled()
    await userEvent.type(field(), 'asked in the second thread')
    expect(sendButton()).toBeDisabled()
    await userEvent.keyboard('{Enter}')
    expect(service.posted()).toEqual([['asked in the first thread', 'gm', 'cnv_1']])
    expect(field()).toHaveValue('asked in the second thread')

    // The first thread's turn settles off screen; now the second one sends.
    await act(async () => service.calls[0].resolve(ANSWER))
    await waitFor(() => expect(sendButton()).toBeEnabled())
    await userEvent.click(field())
    await userEvent.keyboard('{Enter}')
    expect(service.posted()).toEqual([
      ['asked in the first thread', 'gm', 'cnv_1'],
      ['asked in the second thread', 'gm', 'cnv_2'],
    ])
    expect(field()).toHaveValue('')
  })

  it('typing mid-turn announces nothing new and adds no live region: the pane keeps its one', async () => {
    const service = heldService()
    const { container } = render(<ComposerPane nav={{}} post={service.post} />)
    expect(liveRegionsIn(container)).toHaveLength(1)
    await userEvent.type(field(), 'first')
    await userEvent.keyboard('{Enter}')
    const [announcer] = liveRegionsIn(container)
    const announced = announcer.textContent
    expect(announced).toBe('Consulting the tomes…')
    await userEvent.type(field(), 'second{Enter}')
    expect(liveRegionsIn(container)).toHaveLength(1)
    expect(announcer.textContent).toBe(announced)
  })

  it.each(['sage', 'spell', 'rules'] as const)('%s keeps today’s composer: the field and Send wait for the turn', async (mode) => {
    const service = heldService()
    render(<ComposerPane nav={{ mode }} post={service.post} />)
    await userEvent.type(field(), 'What is a basilisk?')
    await userEvent.keyboard('{Enter}')
    expect(service.posted()).toEqual([['What is a basilisk?', mode, 'cnv_1']])
    expect(field()).toBeDisabled()
    expect(sendButton()).toBeDisabled()
    await act(async () => service.calls[0].resolve(ANSWER))
    await waitFor(() => expect(field()).toBeEnabled())
  })

  it('a pane held across the boundary keeps today’s lock, on either side', async () => {
    // A Sage turn held on the chat side after the mode moved to GM.
    const sage = heldService()
    const first = render(<ComposerPane nav={{ mode: 'sage' }} post={sage.post} />)
    await userEvent.type(field(), 'held on the chat side')
    await userEvent.keyboard('{Enter}')
    first.rerender(<ComposerPane nav={{ mode: 'gm' }} post={sage.post} />)
    expect(field()).toBeDisabled()
    await act(async () => sage.calls[0].resolve(ANSWER))
    await waitFor(() => expect(field()).toBeEnabled())
    first.unmount()

    // A GM turn held on the GM side after the mode moved to Sage.
    const gm = heldService()
    const second = render(<ComposerPane nav={{ mode: 'gm' }} post={gm.post} />)
    await userEvent.type(field(), 'held on the GM side')
    await userEvent.keyboard('{Enter}')
    second.rerender(<ComposerPane nav={{ mode: 'sage' }} post={gm.post} />)
    expect(document.querySelector('.assistant-lane')).toHaveAttribute('data-state', 'working')
    expect(field()).toBeDisabled()
    expect(sendButton()).toBeDisabled()
    await act(async () => gm.calls[0].resolve(ANSWER))
    await waitFor(() => expect(field()).toBeEnabled())
  })
})

describe('ChatPane (GM) — session dividers across Load earlier (1kg.3.5, I-10)', () => {
  it('draws a quiet session once, as one span, when Load earlier brings its start above its end', async () => {
    // Newest first on the wire. The first read stops at a full page, whose
    // OLDEST entry is the quiet session's end; its start is on the next page.
    const newest = [chatEntry({ entry_id: 'ent_b', prompt: 'After the quiet week', answer: null }), ...manyChatEntries(98, 1000), QUIET_SESSION[1]]
    const older = [QUIET_SESSION[0], chatEntry({ entry_id: 'ent_a', prompt: 'Before the quiet week', answer: null })]
    const { container } = render(<ComposerPane nav={{}} loadTimeline={pagedTimeline([newest, older])} />)
    expect(await screen.findByText('After the quiet week')).toBeInTheDocument()
    const before = [...container.querySelectorAll('.gm-thread__divider')]
    expect(before.map((divider) => divider.getAttribute('data-boundary'))).toEqual(['end'])

    await userEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    expect(await screen.findByText('Before the quiet week')).toBeInTheDocument()
    const after = [...container.querySelectorAll('.gm-thread__divider')]
    expect(after.map((divider) => divider.getAttribute('data-boundary'))).toEqual(['span'])
    expect(after[0]).toHaveTextContent(DIVIDER_COPY.span)
  })
})
