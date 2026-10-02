/**
 * ChatPane and the Workbench canvas (agent-forge-harness-1kg.6.3, T-15, T-6, C-13, C-20).
 *
 * The GM thread's `Open in canvas` link is an explicit gesture that opens a document;
 * nothing else in the pane ever opens, swaps or closes one (CANVAS-3, CANVAS-4,
 * CANVAS-18). The REAL providers, the real ChatPane, a hydrated thread whose tool
 * result is a document, and a recording server.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { documentResult } from '../gm/laneFixtures'
import { chatEntry, pagedTimeline, toolEntry } from '../gm/threadFixtures'
import {
  defaultWorkbenchRoute, flush, live, mountSelected, run, type Route,
} from '../testing/workbenchHarness'
import type { ChatResponse } from '../api'
import type { PostFn } from '../useChat'
import { ChatPane } from './ChatPane'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'

const THREAD = {
  schema_version: 1, conversation_id: 'cnv_1', campaign_id: 'cmp_A', title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
}

const route: Route = (call) => (call.url === '/conversations/cnv_1' ? { status: 200, body: THREAD } : defaultWorkbenchRoute(call))

const LINK = {
  document_id: 'doc_b', type: 'npc', title: 'Brannoch the Drowned', library_category: 'npcs',
}

const timeline = () =>
  pagedTimeline([
    [
      chatEntry({ entry_id: 'ent_chat0001', prompt: 'Who runs the ferry?' }),
      toolEntry({ entry_id: 'ent_tool0001' }, { tool_id: 'npc', status: 'done', result: documentResult({ document: LINK }) }),
    ],
  ])

const REPLY: ChatResponse = { answer: 'The ferryman, of course.', sources: [], answerable: false }

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

async function mountPane(post?: PostFn) {
  const store = new MemoryConversationStore()
  return mountSelected(
    () => <ChatPane loadTimeline={timeline()} post={post} getAttachments={async () => ({ kind: 'ok', attachments: [] })} />,
    {
      route,
      hash: '#campaign=cmp_A&conversation=cnv_1',
      restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
      wrap: (children) => (
        <ThemeProvider initialTheme="light">
          <ConversationStoreProvider store={store}>{children}</ConversationStoreProvider>
        </ThemeProvider>
      ),
    },
  )
}

const link = (): HTMLElement => screen.getByRole('button', { name: /Brannoch the Drowned/ })

describe('Open in canvas (T-15)', () => {
  it('passes the document link’s id and title to the canvas as a gesture, and the document opens', async () => {
    const user = userEvent.setup()
    const { server } = await mountPane()
    await waitFor(() => expect(link()).toBeInTheDocument())
    expect(server.docCalls()).toHaveLength(0)
    await user.click(link())
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(live.state.doc.kind === 'open' && live.state.doc.document.document_id).toBe('doc_b')
    expect(server.docCalls().map((call) => call.url)).toEqual(['/campaigns/cmp_A/documents/doc_b'])
    expect(live.state.view).toBe('canvas')
    // The link is the opener, so closing returns focus to it.
    expect(live.actions.openerRef.current).toBe(link())
  })

  it('shows the title the link carried while the document loads', async () => {
    const user = userEvent.setup()
    const held: Route = (call) => (/documents/.test(call.url) ? 'defer' : route(call))
    await mountSelected(
      () => <ChatPane loadTimeline={timeline()} getAttachments={async () => ({ kind: 'ok', attachments: [] })} />,
      {
        route: held,
        hash: '#campaign=cmp_A&conversation=cnv_1',
        restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
        wrap: (children) => (
          <ThemeProvider initialTheme="light">
            <ConversationStoreProvider store={new MemoryConversationStore()}>{children}</ConversationStoreProvider>
          </ThemeProvider>
        ),
      },
    )
    await waitFor(() => expect(link()).toBeInTheDocument())
    await user.click(link())
    expect(live.state.doc).toEqual({ kind: 'loading', documentId: 'doc_b', title: 'Brannoch the Drowned' })
  })
})

describe('chat never swaps or closes the document (T-6, CANVAS-3, CANVAS-4, C-20)', () => {
  it('a second document result sits in the thread beside an open document without replacing it, and only its link does', async () => {
    const user = userEvent.setup()
    const { server } = await mountPane()
    await waitFor(() => expect(link()).toBeInTheDocument())
    await run(() => live.actions.openDocument({ documentId: 'doc_a', title: 'Ondrey' }, { gesture: true }))
    expect(live.state.doc.kind === 'open' && live.state.doc.document.document_id).toBe('doc_a')
    const requests = server.docCalls().length
    // The hydrated lane is already on screen with its link: it changed nothing.
    await flush()
    expect(link()).toBeInTheDocument()
    expect(live.state.doc.kind === 'open' && live.state.doc.document.document_id).toBe('doc_a')
    expect(server.docCalls()).toHaveLength(requests)
    // Activating it, on a clean canvas, replaces the document with no dialog.
    await user.click(link())
    await waitFor(() => expect(live.state.doc.kind === 'open' && live.state.doc.document.document_id).toBe('doc_b'))
    expect(live.state.guardDialog).toBeNull()
  })

  it('a GM turn that settles leaves the open document and the network alone', async () => {
    const user = userEvent.setup()
    const post: PostFn = async () => ({ kind: 'ok', response: REPLY })
    const { server } = await mountPane(post)
    await waitFor(() => expect(link()).toBeInTheDocument())
    await run(() => live.actions.openDocument({ documentId: 'doc_a', title: 'Ondrey' }, { gesture: true }))
    const documents = server.docCalls().length
    await user.type(screen.getByPlaceholderText('Ask…'), 'And the toll?')
    await user.keyboard('{Enter}')
    expect(await screen.findByText('The ferryman, of course.')).toBeInTheDocument()
    await flush()
    expect(live.state.doc.kind === 'open' && live.state.doc.document.document_id).toBe('doc_a')
    expect(server.docCalls()).toHaveLength(documents)
    expect(server.calls.filter((call) => call.method !== 'GET')).toEqual([])
    expect(live.state.view).toBe('canvas')
  })

  it('a conversation switch and a history load do not close it either', async () => {
    const { server } = await mountPane()
    await waitFor(() => expect(link()).toBeInTheDocument())
    await run(() => live.actions.openDocument({ documentId: 'doc_a', title: 'Ondrey' }, { gesture: true }))
    const documents = server.docCalls().length
    act(() => live.nav.setConversationId(null))
    await flush()
    expect(live.state.doc.kind).toBe('open')
    expect(server.docCalls()).toHaveLength(documents)
  })
})

describe('render cost (C-13)', () => {
  it('ChatPane renders no more often for a document that loads, opens, is replaced and closes', async () => {
    const renders = vi.fn()
    const store = new MemoryConversationStore()
    await mountSelected(
      () => (
        <React.Profiler id="chat" onRender={renders}>
          <ChatPane loadTimeline={timeline()} getAttachments={async () => ({ kind: 'ok', attachments: [] })} />
        </React.Profiler>
      ),
      {
        route,
        hash: '#campaign=cmp_A&conversation=cnv_1',
        restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
        wrap: (children) => (
          <ThemeProvider initialTheme="light">
            <ConversationStoreProvider store={store}>{children}</ConversationStoreProvider>
          </ThemeProvider>
        ),
      },
    )
    await waitFor(() => expect(link()).toBeInTheDocument())
    await flush()
    const settled = renders.mock.calls.length
    await run(() => live.actions.openDocument({ documentId: 'doc_a', title: 'Ondrey' }, { gesture: true }))
    await run(() => live.actions.openDocument({ documentId: 'doc_b', title: 'Brannoch' }, { gesture: true }))
    await run(() => live.actions.closeDocument())
    await flush()
    expect(live.state.doc.kind).toBe('closed')
    expect(renders.mock.calls.length).toBe(settled)
  })
})

describe('the composer and the conversation are handed to the canvas (CANVAS-32, C-14)', () => {
  it('closing a document that nothing opened by gesture returns focus to the composer', async () => {
    const { server } = await mountPane()
    await waitFor(() => expect(link()).toBeInTheDocument())
    await run(() => live.actions.openDocument({ documentId: 'doc_a' }, { gesture: false }))
    await run(() => live.actions.closeDocument())
    expect(live.actions.composerRef.current).toBe(screen.getByPlaceholderText('Ask…'))
    expect(server.docCalls()).toHaveLength(1)
  })

  it('exposes the Conversation region as the shell’s skip target', async () => {
    await mountPane()
    await waitFor(() => expect(link()).toBeInTheDocument())
    expect(live.actions.chatRegionRef.current).toBe(screen.getByRole('region', { name: 'Conversation' }))
  })
})
