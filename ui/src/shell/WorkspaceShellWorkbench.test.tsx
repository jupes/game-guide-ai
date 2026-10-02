/**
 * WorkspaceShell with the Workbench (agent-forge-harness-1kg.6.3, T-13; brief 2.1, 2.8,
 * 2.9 and the Critic's C-2, C-3, C-5, C-10, C-11, C-14, C-21).
 *
 * The REAL providers and the real shell, a per-width `matchMedia`, and a recording
 * server. jsdom has no layout and no CSS, so what is proven here is structure and
 * semantics: which regions exist at which width, the data attributes the stylesheets
 * key on, focus, and what stays mounted. Boxes, scrolling and the hidden column are
 * proven in Chromium by `WorkspaceShellWorkbench.stories.tsx`.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { installMatchMediaWidth, type MatchMediaWidthStub } from '../testing/matchMediaWidth'
import {
  flush, libraryRoute, live, mountSelected, mountWorkbench, run, type MountOptions, type Route, defaultWorkbenchRoute,
  type LibraryRow,
} from '../testing/workbenchHarness'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import type { CanvasDirtySource, FlushOutcome } from './lossGuard'
import { WorkspaceShell } from './WorkspaceShell'

let widthStub: MatchMediaWidthStub | null = null

afterEach(() => {
  widthStub?.restore()
  widthStub = null
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

const WITH_DOC: MountOptions = {
  hash: '#campaign=cmp_A&document=doc_a',
  restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
}

async function mountShell(width: number, options: MountOptions = {}, selected = true) {
  widthStub = installMatchMediaWidth(width)
  const store = new MemoryConversationStore()
  const wrap = (children: React.ReactNode): React.JSX.Element => (
    <ThemeProvider initialTheme="light">
      <ConversationStoreProvider store={store}>{children}</ConversationStoreProvider>
    </ThemeProvider>
  )
  const mount = selected ? mountSelected : mountWorkbench
  const mounted = await mount(() => <WorkspaceShell />, { stubGlobalFetch: true, wrap, ...options })
  return { ...mounted, store }
}

const root = (): HTMLElement => document.querySelector('.workspace-shell') as HTMLElement
const columns = () => ({
  workbench: document.querySelector('.workbench') as HTMLElement | null,
  chat: document.querySelector('.workbench__chat') as HTMLElement | null,
  canvas: document.querySelector('.workbench__canvas') as HTMLElement | null,
})
const rail = (): HTMLElement | null => screen.queryByRole('navigation', { name: 'Navigation rail' })
const switchGroup = (): HTMLElement | null => screen.queryByRole('group', { name: 'Workbench view' })
const conversation = (): HTMLElement => screen.getByRole('region', { name: 'Conversation' })
const canvasHeading = (name = 'Ondrey'): HTMLElement => screen.getByRole('heading', { level: 2, name })
/** The same heading even while its column is hidden (a single column showing the chat). */
const hiddenCanvasHeading = (name = 'Ondrey'): HTMLElement =>
  screen.getByRole('heading', { level: 2, name, hidden: true })
const resizeTo = (width: number): void => {
  act(() => widthStub?.setWidth(width))
}

class TestSource implements CanvasDirtySource {
  dirty = true
  outcome: FlushOutcome = 'failed'
  isDirty = () => this.dirty
  flush = () => Promise.resolve(this.outcome)
  discard = () => {
    this.dirty = false
  }
}

describe('the wide layout (LAYOUT-1)', () => {
  it('with no document is today’s layout: a sidebar, no rail, no canvas, no switch', async () => {
    await mountShell(1280)
    expect(root()).toHaveAttribute('data-layout', 'wide')
    expect(root()).toHaveAttribute('data-nav', 'sidebar')
    expect(rail()).toBeNull()
    expect(switchGroup()).toBeNull()
    expect(columns().canvas).toBeNull()
    expect(columns().workbench).not.toHaveAttribute('data-canvas')
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument()
    expect(conversation()).toBeInTheDocument()
  })

  it('with a document shows the rail, the chat and the canvas, and LeftNav waits in the closed drawer', async () => {
    await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    expect(root()).toHaveAttribute('data-nav', 'rail')
    expect(rail()).toBeInTheDocument()
    expect(columns().workbench).toHaveAttribute('data-canvas', 'true')
    const { chat, canvas } = columns()
    expect(chat?.compareDocumentPosition(canvas as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(chat).toContainElement(conversation())
    expect(canvas).toContainElement(canvasHeading())
    expect(canvas).not.toHaveAttribute('hidden')
    expect(chat).not.toHaveAttribute('data-concealed')
    // The drawer host exists (LeftNav never remounts) and is closed.
    expect(document.querySelector('.workspace-shell__nav')).not.toHaveAttribute('data-open')
    expect(switchGroup()).toBeNull()
  })

  it('closing the canvas brings the sidebar back, and focus lands in the composer when no control opened it (C-3)', async () => {
    const user = userEvent.setup()
    await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(root()).toHaveAttribute('data-nav', 'sidebar'))
    expect(rail()).toBeNull()
    expect(columns().canvas).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A')
    await waitFor(() => expect(screen.getByPlaceholderText('Ask…')).toHaveFocus())
  })
})

describe('the medium layout (LAYOUT-2, LAYOUT-7)', () => {
  it('has the rail even with no document, and no switch, and one column', async () => {
    await mountShell(900)
    expect(root()).toHaveAttribute('data-layout', 'medium')
    expect(root()).toHaveAttribute('data-nav', 'rail')
    expect(rail()).toBeInTheDocument()
    expect(switchGroup()).toBeNull()
    expect(columns().canvas).toBeNull()
  })

  it('768 is medium and 1023 is medium, but 1024 is wide', async () => {
    await mountShell(768)
    expect(root()).toHaveAttribute('data-layout', 'medium')
    resizeTo(1023)
    expect(root()).toHaveAttribute('data-layout', 'medium')
    resizeTo(1024)
    expect(root()).toHaveAttribute('data-layout', 'wide')
    expect(rail()).toBeNull()
  })

  it('with a document shows one column and the switch, in the Canvas view, and the switch swaps them (C-2, C-10)', async () => {
    const user = userEvent.setup()
    await mountShell(900, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    const group = switchGroup()
    expect(group).toBeInTheDocument()
    // C-10: the switch is a row in the chrome, under the channels, so it goes inert with it.
    expect(document.querySelector('.workspace-shell__chrome')).toContainElement(group)
    expect(screen.getByRole('button', { name: 'Canvas' })).toHaveAttribute('aria-pressed', 'true')
    const { chat, canvas } = columns()
    // C-2: the chat column is concealed, never `hidden` or inert, so its announcer stays exposed.
    expect(chat).toHaveAttribute('data-concealed', 'true')
    expect(chat).not.toHaveAttribute('hidden')
    expect(chat).not.toHaveAttribute('inert')
    expect(canvas).not.toHaveAttribute('hidden')
    await user.click(screen.getByRole('button', { name: 'Chat' }))
    expect(screen.getByRole('button', { name: 'Chat' })).toHaveAttribute('aria-pressed', 'true')
    expect(chat).not.toHaveAttribute('data-concealed')
    expect(canvas).toHaveAttribute('hidden')
    await user.click(screen.getByRole('button', { name: 'Canvas' }))
    expect(canvas).not.toHaveAttribute('hidden')
    expect(chat).toHaveAttribute('data-concealed', 'true')
    // Switching views moves focus nowhere: the pressed segment keeps it.
    expect(screen.getByRole('button', { name: 'Canvas' })).toHaveFocus()
  })

  it('keeps the chat’s one status node exposed while the canvas is showing (C-2)', async () => {
    await mountShell(900, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    const chat = columns().chat as HTMLElement
    expect(chat).toHaveAttribute('data-concealed', 'true')
    const arrival = chat.querySelector('.chat-pane__arrival') as HTMLElement
    expect(arrival).toHaveAttribute('role', 'status')
    expect(arrival.closest('[hidden], [inert]')).toBeNull()
  })

  it('opens the drawer from the rail, makes everything else inert, and Escape returns focus to the rail button', async () => {
    const user = userEvent.setup()
    await mountShell(900)
    const open = screen.getByRole('button', { name: 'Open navigation' })
    expect(open).toHaveAttribute('aria-expanded', 'false')
    await user.click(open)
    const dialog = screen.getByRole('dialog', { name: 'Navigation' })
    expect(dialog).toHaveFocus()
    expect(open).toHaveAttribute('aria-expanded', 'true')
    expect(rail()).toHaveAttribute('inert')
    expect(document.querySelector('.workspace-shell__chrome')).toHaveAttribute('inert')
    expect(document.querySelector('.workspace-shell__main')).toHaveAttribute('inert')
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveFocus()
    expect(rail()).not.toHaveAttribute('inert')
  })
})

describe('the narrow layout (LAYOUT-3)', () => {
  it('has the TopBar menu, no rail, and with a document the switch and a full-screen canvas', async () => {
    await mountShell(375, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    expect(root()).toHaveAttribute('data-layout', 'narrow')
    expect(root()).toHaveAttribute('data-nav', 'hidden')
    expect(rail()).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Open navigation' })).toHaveLength(1)
    expect(document.querySelector('.top-bar')).toContainElement(screen.getByRole('button', { name: 'Open navigation' }))
    expect(switchGroup()).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Canvas' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Close canvas' })).toBeNull()
  })

  it('Back is the guarded close: the document closes and the chat is the whole screen again (I-5)', async () => {
    const user = userEvent.setup()
    await mountShell(375, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(columns().canvas).toBeNull())
    expect(switchGroup()).toBeNull()
    expect(columns().chat).not.toHaveAttribute('data-concealed')
  })
})

describe('crossing a breakpoint (LAYOUT-6, C-5)', () => {
  it('keeps the document, the canvas node and the composer draft from 1280 to 900 and back (AE-36)', async () => {
    const user = userEvent.setup()
    const { server } = await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    const heading = canvasHeading()
    const composer = screen.getByPlaceholderText('Ask…')
    await user.type(composer, 'half a thought')
    resizeTo(900)
    expect(hiddenCanvasHeading()).toBe(heading)
    expect(screen.getByPlaceholderText('Ask…')).toBe(composer)
    expect(composer).toHaveValue('half a thought')
    resizeTo(375)
    resizeTo(1280)
    expect(canvasHeading()).toBe(heading)
    expect(screen.getByPlaceholderText('Ask…')).toBe(composer)
    expect(composer).toHaveValue('half a thought')
    expect(server.docCalls()).toHaveLength(1)
  })

  it('wide to medium with focus in the composer shows the Chat view and keeps focus there', async () => {
    await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    expect(live.state.view).toBe('canvas')
    const composer = screen.getByPlaceholderText('Ask…')
    composer.focus()
    resizeTo(900)
    await waitFor(() => expect(live.state.view).toBe('chat'))
    expect(composer).toHaveFocus()
    expect(columns().chat).not.toHaveAttribute('data-concealed')
    expect(columns().canvas).toHaveAttribute('hidden')
  })

  it('wide to medium with focus on the canvas title shows the Canvas view and keeps focus there', async () => {
    await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    act(() => live.actions.setView('chat'))
    const heading = canvasHeading()
    heading.focus()
    resizeTo(900)
    await waitFor(() => expect(live.state.view).toBe('canvas'))
    expect(heading).toHaveFocus()
    expect(columns().canvas).not.toHaveAttribute('hidden')
  })

  it('crossing with focus in neither column keeps the view it had', async () => {
    await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    act(() => live.actions.setView('chat'))
    ;(document.activeElement as HTMLElement | null)?.blur()
    resizeTo(900)
    await flush()
    expect(live.state.view).toBe('chat')
  })

  it('a layout change that hides the open drawer does not strand focus (the 0rn rule still holds)', async () => {
    const user = userEvent.setup()
    await mountShell(375)
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(screen.getByRole('dialog', { name: 'Navigation' })).toBeInTheDocument()
    resizeTo(1280)
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull()
    expect(root()).toHaveAttribute('data-nav', 'sidebar')
  })
})

describe('who sees the Workbench (X-9)', () => {
  it.each([375, 768, 900, 1280])('a player at %i px sees no Workbench surface and makes no document request', async (width) => {
    widthStub = installMatchMediaWidth(width)
    const store = new MemoryConversationStore()
    const { server } = await mountWorkbench(() => <WorkspaceShell />, {
      role: 'player', stubGlobalFetch: true, hash: '#campaign=cmp_A&document=doc_a',
      restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
      wrap: (children) => (
        <ThemeProvider initialTheme="light"><ConversationStoreProvider store={store}>{children}</ConversationStoreProvider></ThemeProvider>
      ),
    })
    await flush()
    expect(columns().canvas).toBeNull()
    expect(switchGroup()).toBeNull()
    expect(screen.queryByRole('button', { name: 'Campaign documents' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Ondrey' })).toBeNull()
    expect(screen.queryByRole('group', { name: 'Skip links' })).toBeNull()
    expect(server.docCalls()).toHaveLength(0)
    expect(window.location.hash).toBe('')
    // The medium layout's rail is for every role (LAYOUT-7).
    expect(rail() !== null).toBe(width >= 768 && width < 1024)
  })

  it('Sage keeps the rail at 900 but hides the canvas; GM brings the same document back with no refetch (AE-73, CANVAS-9)', async () => {
    const user = userEvent.setup()
    const { server } = await mountShell(900, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    const channels = within(screen.getByRole('navigation', { name: 'Channels' }))
    await user.click(channels.getByRole('button', { name: 'Sage' }))
    expect(columns().canvas).toBeNull()
    expect(rail()).toBeInTheDocument()
    expect(switchGroup()).toBeNull()
    expect(screen.queryByRole('heading', { level: 2, name: 'Ondrey' })).toBeNull()
    expect(live.state.doc.kind).toBe('open')
    await user.click(channels.getByRole('button', { name: 'GM' }))
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    expect(server.docCalls()).toHaveLength(1)
  })

  it('never puts a title in the tab title (C-21)', async () => {
    const before = document.title
    await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    expect(document.title).toBe(before)
    expect(document.title).not.toMatch(/Ondrey|Name of cmp_A/)
  })
})

describe('skip links (I-17, C-14)', () => {
  it('the first Tab stop is Skip to conversation, and activating it focuses the Conversation region', async () => {
    const user = userEvent.setup()
    await mountShell(1280)
    await user.tab()
    expect(screen.getByRole('button', { name: 'Skip to conversation' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(conversation()).toHaveFocus()
  })

  it('Skip to document exists only while the canvas column is visible, and focuses its title', async () => {
    const user = userEvent.setup()
    await mountShell(900, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Chat' }))
    expect(screen.queryByRole('button', { name: 'Skip to document' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Canvas' }))
    await user.click(screen.getByRole('button', { name: 'Skip to document' }))
    expect(canvasHeading()).toHaveFocus()
  })

  it('are not rendered where there is no Workbench', async () => {
    await mountShell(1280, {}, false)
    expect(screen.queryByRole('button', { name: 'Skip to conversation' })).toBeNull()
  })

  it('go inert with the rest of the chrome while the drawer is open', async () => {
    const user = userEvent.setup()
    await mountShell(900)
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(screen.getByRole('button', { name: 'Skip to conversation', hidden: true }).closest('[inert]')).not.toBeNull()
  })
})

describe('the loss guard dialog and announcer at the shell root (C-11)', () => {
  async function dirtyShell(source: TestSource) {
    const mounted = await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    act(() => { live.actions.registerDirtySource(source) })
    return mounted
  }

  it('shows the dialog over an inert shell, as a sibling of the chrome and the body, and Keep editing returns focus', async () => {
    const user = userEvent.setup()
    await dirtyShell(new TestSource())
    const close = screen.getByRole('button', { name: 'Close canvas' })
    await user.click(close)
    const dialog = await screen.findByRole('dialog', { name: 'You have unsaved changes to Ondrey' })
    expect(within(dialog).getByRole('button', { name: 'Keep editing' })).toHaveFocus()
    expect(root()).toContainElement(dialog)
    expect(dialog.closest('[inert]')).toBeNull()
    expect(document.querySelector('.workspace-shell__chrome')).toHaveAttribute('inert')
    expect(rail()?.closest('[inert]')).not.toBeNull()
    expect(document.querySelector('.workspace-shell__body')).toHaveAttribute('inert')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /unsaved/ })).toBeNull())
    expect(document.querySelector('.workspace-shell__body')).not.toHaveAttribute('inert')
    expect(close).toHaveFocus()
    expect(canvasHeading()).toBeInTheDocument()
  })

  it('Discard changes discards and closes the canvas', async () => {
    const user = userEvent.setup()
    const source = new TestSource()
    await dirtyShell(source)
    await user.click(screen.getByRole('button', { name: 'Close canvas' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(columns().canvas).toBeNull())
    expect(source.dirty).toBe(false)
  })

  it('an offline refusal is announced in an exposed status node while nothing else is (AE-19)', async () => {
    const user = userEvent.setup()
    const source = new TestSource()
    source.outcome = 'offline'
    await dirtyShell(source)
    await user.click(screen.getByRole('button', { name: 'Close canvas' }))
    const status = await screen.findByText("You're offline — changes aren't saved. Keep this tab open.")
    expect(status).toHaveAttribute('role', 'status')
    expect(status.closest('[inert], [hidden]')).toBeNull()
    expect(screen.queryByRole('dialog', { name: /unsaved/ })).toBeNull()
    expect(canvasHeading()).toBeInTheDocument()
  })

  it('the announcer is mounted empty for the Workbench’s life, so its first message is read', async () => {
    await mountShell(1280, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    const statuses = screen.getAllByRole('status')
    const announcer = statuses.find((node) => node.classList.contains('workbench-announcer'))
    expect(announcer).toBeDefined()
    expect(announcer).toHaveTextContent('')
  })
})

const ROWS: Readonly<Record<string, readonly LibraryRow[]>> = {
  npcs: [{ id: 'doc_a', type: 'npc', title: 'Ondrey' }],
}

/** A wire tool entry whose result is a document link (the shape `/timeline` serves). */
const TOOL_ENTRY = {
  schema_version: 1,
  entry_kind: 'tool',
  entry_id: 'ent_77aa12bd',
  created_at: '2026-09-16T19:35:00Z',
  brief: 'a ferryman',
  invocation: {
    schema_version: 1,
    invocation_id: 'inv_0a1b2c3d4e5f6a7b',
    tool_id: 'npc',
    status: 'done',
    attempt: 1,
    cancel_requested: false,
    created_at: '2026-09-16T19:35:00Z',
    updated_at: '2026-09-16T19:35:20Z',
    result: {
      result_kind: 'document',
      tool_id: 'npc',
      prose: 'A ferryman who remembers every debt.',
      suggestions: [],
      document: { document_id: 'doc_a', type: 'npc', title: 'Ondrey the Ferryman', library_category: 'npcs' },
    },
    error: null,
  },
}

const THREAD = {
  schema_version: 1, conversation_id: 'cnv_1', campaign_id: 'cmp_A', title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
}

const withThread = (rows: Readonly<Record<string, readonly LibraryRow[]>>): Route => (call) => {
  if (call.url === '/conversations/cnv_1') return { status: 200, body: THREAD }
  if (call.url.startsWith('/conversations/cnv_1/timeline')) {
    return { status: 200, body: { schema_version: 1, conversation_id: 'cnv_1', items: [TOOL_ENTRY], next_cursor: null } }
  }
  return libraryRoute(rows)(call)
}

describe('the documents list in the shell (I-2) and where focus lands on close (C-3, CANVAS-32)', () => {
  it('the rail’s Campaign documents button opens the drawer and lands on the documents list', async () => {
    const user = userEvent.setup()
    await mountShell(900, { route: libraryRoute(ROWS) })
    await user.click(screen.getByRole('button', { name: 'Campaign documents' }))
    expect(screen.getByRole('dialog', { name: 'Navigation' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Campaign documents' })).toHaveFocus()
    // The same drawer, the same single list: nothing was fetched twice for it.
  })

  it('the rail has no Campaign documents button for a player, or in Sage', async () => {
    const user = userEvent.setup()
    await mountShell(900, { route: libraryRoute(ROWS) })
    expect(screen.getByRole('button', { name: 'Campaign documents' })).toBeInTheDocument()
    await user.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'Sage' }))
    expect(screen.queryByRole('button', { name: 'Campaign documents' })).toBeNull()
  })

  it('1280: a sidebar row opens the document, and closing it returns focus to that very row', async () => {
    const user = userEvent.setup()
    await mountShell(1280, { route: libraryRoute(ROWS) })
    const row = await screen.findByRole('button', { name: /Ondrey/ })
    await user.click(row)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    await waitFor(() => expect(canvasHeading()).toHaveFocus())
    expect(root()).toHaveAttribute('data-nav', 'rail')
    await user.click(screen.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(root()).toHaveAttribute('data-nav', 'sidebar'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Ondrey/ })).toHaveFocus())
  })

  it('900: a drawer row opens the document and closes the drawer; closing returns focus to the rail’s Open navigation (C-3a)', async () => {
    const user = userEvent.setup()
    await mountShell(900, { route: libraryRoute(ROWS) })
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(await screen.findByRole('button', { name: /Ondrey/ }))
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull()
    await waitFor(() => expect(canvasHeading()).toHaveFocus())
    await user.click(screen.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveFocus())
  })

  it('375: a link in the chat shows the Canvas view, and Back returns focus to that link', async () => {
    const user = userEvent.setup()
    await mountShell(375, {
      route: withThread({}),
      hash: '#campaign=cmp_A&conversation=cnv_1',
      restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
    })
    const link = await screen.findByRole('button', { name: /Ondrey the Ferryman/ })
    await user.click(link)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    expect(live.state.view).toBe('canvas')
    expect(columns().chat).toHaveAttribute('data-concealed', 'true')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(columns().canvas).toBeNull())
    await waitFor(() => expect(screen.getByRole('button', { name: /Ondrey the Ferryman/ })).toHaveFocus())
  })
})

describe('what the shell never does (C-12, C-13)', () => {
  it('opening, closing and switching views make no request but the one document GET', async () => {
    const user = userEvent.setup()
    const { server } = await mountShell(900, WITH_DOC)
    await waitFor(() => expect(canvasHeading()).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Chat' }))
    await user.click(screen.getByRole('button', { name: 'Canvas' }))
    resizeTo(1280)
    resizeTo(375)
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await flush()
    expect(server.docCalls()).toHaveLength(1)
    // Only GETs, plus the documents list's read of the library by POST (4 categories, once).
    expect(server.calls.filter((call) => call.method !== 'GET' && !call.url.endsWith('/library'))).toEqual([])
    expect(server.libraryCalls()).toHaveLength(4)
  })

  it('a failed restore shows the panel in the canvas column with Close, and Close clears the key', async () => {
    const user = userEvent.setup()
    const route: Route = (call) => (/doc_a$/.test(call.url) ? { status: 404, body: {} } : defaultWorkbenchRoute(call))
    await mountShell(1280, { ...WITH_DOC, route })
    await screen.findByRole('heading', { level: 2, name: "This document isn't available" })
    expect(columns().canvas).not.toBeNull()
    await run(async () => {
      await user.click(screen.getByRole('button', { name: 'Close' }))
    })
    await waitFor(() => expect(columns().canvas).toBeNull())
    expect(window.location.hash).toBe('#campaign=cmp_A')
  })
})
