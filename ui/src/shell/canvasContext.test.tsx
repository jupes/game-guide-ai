/**
 * canvasContext.test.tsx -- the Workbench canvas state against the REAL AppNav,
 * CurrentUser and Campaign providers (agent-forge-harness-1kg.6.3; brief sections
 * 2.2-2.5, T-5, T-6, T-8 and the Critic's C-3, C-4, C-5, C-12, C-13, C-17).
 *
 * Every request goes through a recording stub that can hold an answer back
 * (`defer`), so each race below really interleaves. The surface is a few plain
 * elements, not CanvasHost: what is proven here is what the PROVIDER decides.
 */

import * as React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import * as api from '../api'
import { CampaignSchema, type Campaign } from '../gm/contracts'
import { DOCUMENT_FIXTURES } from '../gm/documentFixtures'
import { AppNavProvider, useAppNav, type AppNavState } from './AppNav'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './currentUser'
import {
  CampaignProvider, useCampaign, useCampaignDocument, type CampaignContextValue, type CampaignDocumentValue,
} from './campaignContext'
import {
  CanvasProvider, canvasShown, useCanvasActions, useCanvasState, useWorkbenchActive, type CanvasActions,
  type CanvasState,
} from './canvasContext'
import type { IdentityChannelLike } from './identityBroadcast'
import { FLUSH_TIMEOUT_MS, type CanvasDirtySource, type FlushOutcome } from './lossGuard'
import type { CampaignRestore } from './workspaceFragment'

// ── Fixtures and harness ──────────────────────────────────────────────────────

const ADA = 'ada@example.com'

function campaign(id: string): Campaign {
  return CampaignSchema.parse({
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
    avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
  })
}

const NAMES: Record<string, string> = { doc_a: 'Ondrey', doc_b: 'Brannoch', doc_c: 'Cressa' }

function docBody(campaignId: string, id: string): Record<string, unknown> {
  const base = DOCUMENT_FIXTURES.npc
  return {
    ...base, document_id: id, campaign_id: campaignId,
    data: { ...(base.data as Record<string, unknown>), name: NAMES[id] ?? id },
  }
}

type Reply = { status: number; body?: unknown; raw?: string } | 'network' | 'abort'
interface Call { url: string; method: string; reply: (r: Reply) => void }
type Route = (call: Call) => Reply | 'defer'

const defaultRoute: Route = ({ url }) => {
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (one !== null) return { status: 200, body: campaign(one[1]) }
  const doc = /^\/campaigns\/(cmp_\w+)\/documents\/(doc_\w+)$/.exec(url)
  if (doc !== null) {
    if (doc[2] === 'doc_missing') return { status: 404, body: { code: 'not_found' } }
    if (doc[2] === 'doc_newer') return { status: 200, body: { ...docBody(doc[1], doc[2]), schema_version: 2 } }
    return { status: 200, body: docBody(doc[1], doc[2]) }
  }
  return { status: 404, body: {} }
}

function stubServer(route: Route = defaultRoute) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET',
      reply: (r) => {
        if (r === 'network') reject(new TypeError('Failed to fetch'))
        else if (r === 'abort') reject(new DOMException('aborted', 'AbortError'))
        else resolve(new Response(r.raw ?? JSON.stringify(r.body ?? {}), { status: r.status }))
      },
    }
    calls.push(call)
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  const lines = () => calls.map((c) => `${c.method} ${c.url}`)
  const docCalls = () => calls.filter((c) => /\/documents\//.test(c.url))
  return { fetchImpl, calls, lines, docCalls }
}

interface FakeChannel extends IdentityChannelLike { posts: unknown[]; closed: boolean }
function channels() {
  const opened: FakeChannel[] = []
  const factory = (): FakeChannel => {
    const channel: FakeChannel = {
      posts: [], closed: false, onmessage: null,
      postMessage: (message) => { channel.posts.push(message) },
      close: () => { channel.closed = true },
    }
    opened.push(channel)
    return channel
  }
  const receive = (data: unknown) => act(() => {
    for (const c of opened) c.onmessage?.(new MessageEvent('message', { data }))
  })
  return { factory, receive }
}

/** One entry per committed Surface render: the campaign of the document it showed, and the campaign selected then. */
const rendered: Array<{ shows: string | null; selected: string | null }> = []
const live = {} as {
  c: CampaignContextValue; d: CampaignDocumentValue; nav: AppNavState; user: CurrentUserContextValue
  s: CanvasState; a: CanvasActions; active: boolean
}

/** What a canvas column would render: a heading the provider can focus, and nothing for `loading`. */
function Surface(): React.JSX.Element {
  const s = useCanvasState()
  const a = useCanvasActions()
  const { titleRef, composerRef } = a
  const c = useCampaign()
  const d = useCampaignDocument()
  const nav = useAppNav()
  const user = useCurrentUser()
  const active = useWorkbenchActive()
  React.useLayoutEffect(() => {
    live.s = s
    live.a = a
    live.c = c
    live.d = d
    live.nav = nav
    live.user = user
    live.active = active
    rendered.push({ shows: s.doc.kind === 'open' ? s.doc.document.campaign_id : null, selected: c.scope?.campaignId ?? null })
  })
  const { doc } = s
  const heading = doc.kind === 'open' ? String(doc.document.data.name) : doc.kind === 'closed' || doc.kind === 'loading' ? null : doc.kind
  return (
    <div>
      <button type="button" id="opener">A link in the chat</button>
      <input aria-label="composer" ref={composerRef as React.RefObject<HTMLInputElement | null>} />
      <p data-testid="state">{`${doc.kind}|${s.view}|${canvasShown(active, doc)}`}</p>
      {doc.kind === 'loading' && <p>{`Opening ${doc.title ?? 'the document'}`}</p>}
      {heading !== null && <h2 ref={titleRef} tabIndex={-1}>{heading}</h2>}
    </div>
  )
}

interface MountOptions {
  role?: 'dm' | 'player'; restore?: CampaignRestore; route?: Route; hash?: string; strict?: boolean
  mode?: 'gm' | 'sage'
}
async function mount(options: MountOptions = {}) {
  const server = stubServer(options.route)
  const signal = channels()
  window.history.replaceState(null, '', `/workspace${options.hash ?? ''}`)
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: ADA, role: options.role ?? 'dm' } })
  const tree = (
    <AppNavProvider initialScreen="workspace" initialMode={options.mode ?? 'gm'}>
      <CurrentUserProvider identityChannelFactory={signal.factory}>
        <CampaignProvider fetchImpl={server.fetchImpl} restore={options.restore ?? null}>
          <CanvasProvider fetchImpl={server.fetchImpl}>
            <Surface />
          </CanvasProvider>
        </CampaignProvider>
      </CurrentUserProvider>
    </AppNavProvider>
  )
  const view = render(options.strict === true ? <React.StrictMode>{tree}</React.StrictMode> : tree)
  await waitFor(() => expect(live.user.authStatus).not.toBe('checking'))
  return { server, signal, view }
}

/** A dm mounted with campaign A selected and the canvas closed. */
async function mountSelected(options: MountOptions = {}) {
  const mounted = await mount({
    hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null }, ...options,
  })
  await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
  return mounted
}

const flush = () => act(async () => {})
const run = <T,>(fn: () => Promise<T>): Promise<T> => act(fn)
const stateText = (): string => screen.getByTestId('state').textContent ?? ''
const open = (documentId: string, gesture = true, title: string | null = null) =>
  live.a.openDocument({ documentId, title }, { gesture })
const heading = (name: string) => screen.queryByRole('heading', { name })

class TestSource implements CanvasDirtySource {
  dirty = true
  outcome: FlushOutcome = 'saved'
  flushes = 0
  discards = 0
  isDirty = () => this.dirty
  flush = () => {
    this.flushes += 1
    if (this.outcome === 'saved') this.dirty = false
    return Promise.resolve(this.outcome)
  }
  discard = () => {
    this.discards += 1
    this.dirty = false
  }
}

beforeEach(() => {
  vi.useRealTimers()
  rendered.length = 0
})
afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

// ── Who sees the Workbench (X-9) ──────────────────────────────────────────────

describe('useWorkbenchActive (2.2)', () => {
  it('is true only for a dm, in the GM channel, with a campaign selected', async () => {
    await mountSelected()
    expect(live.active).toBe(true)
    act(() => live.nav.setMode('sage'))
    expect(live.active).toBe(false)
    act(() => live.nav.setMode('gm'))
    expect(live.active).toBe(true)
    await run(() => live.c.clearCampaign())
    expect(live.active).toBe(false)
  })

  it('is false while a campaign is only restoring, and for a player', async () => {
    await mount({
      hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null },
      route: (call) => (call.url === '/campaigns/cmp_A' ? 'defer' : defaultRoute(call)),
    })
    await flush()
    expect(live.c.selection.kind).toBe('restoring')
    expect(live.active).toBe(false)
  })

  it('is false for a player, who has no campaign layer at all', async () => {
    await mount({ role: 'player' })
    expect(live.active).toBe(false)
    expect(canvasShown(live.active, live.s.doc)).toBe(false)
  })

  it('canvasShown needs both the Workbench and a document', () => {
    expect(canvasShown(true, { kind: 'closed' })).toBe(false)
    expect(canvasShown(true, { kind: 'loading', documentId: 'doc_a', title: null })).toBe(true)
    expect(canvasShown(false, { kind: 'loading', documentId: 'doc_a', title: null })).toBe(false)
  })

  it('outside a provider the canvas value is inert and a call changes nothing', async () => {
    const held: { a?: CanvasActions; s?: CanvasState } = {}
    function Bare(): null {
      const a = useCanvasActions()
      const s = useCanvasState()
      React.useLayoutEffect(() => {
        held.a = a
        held.s = s
      })
      return null
    }
    render(<Bare />)
    expect(held.s?.doc).toEqual({ kind: 'closed' })
    await expect(held.a?.openDocument({ documentId: 'doc_a' }, { gesture: true })).resolves.toBeUndefined()
    await expect(held.a?.closeDocument()).resolves.toBe(true)
    expect(() => held.a?.retry()).not.toThrow()
    expect(held.a?.registerDirtySource(new TestSource())).toBeTypeOf('function')
  })
})

// ── Open, replace, restore, close (T-5) ───────────────────────────────────────

describe('opening a document (T-5, CANVAS-5..7)', () => {
  it('a link goes loading, then open, moves focus to the title and writes the fragment', async () => {
    const { server } = await mountSelected({
      route: (call) => (/documents/.test(call.url) ? 'defer' : defaultRoute(call)),
    })
    const opener = document.getElementById('opener') as HTMLElement
    opener.focus()
    act(() => { void open('doc_a', true, 'Ondrey') })
    expect(live.s.doc).toEqual({ kind: 'loading', documentId: 'doc_a', title: 'Ondrey' })
    expect(live.s.view).toBe('canvas')
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
    expect(stateText()).toBe('loading|canvas|true')
    act(() => server.docCalls()[0].reply({ status: 200, body: docBody('cmp_A', 'doc_a') }))
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    await waitFor(() => expect(heading('Ondrey')).toHaveFocus())
    expect(server.docCalls().map((c) => `${c.method} ${c.url}`)).toEqual(['GET /campaigns/cmp_A/documents/doc_a'])
  })

  it('records the opener the gesture came from, for the close to return to (CANVAS-32)', async () => {
    await mountSelected()
    const opener = document.getElementById('opener') as HTMLElement
    opener.focus()
    await run(() => open('doc_a'))
    expect(live.a.openerRef.current).toBe(opener)
  })

  it('the same document again fetches nothing and focuses the title (CANVAS-5)', async () => {
    const { server } = await mountSelected()
    await run(() => open('doc_a'))
    await waitFor(() => expect(heading('Ondrey')).toHaveFocus())
    const opener = document.getElementById('opener') as HTMLElement
    opener.focus()
    await run(() => open('doc_a'))
    expect(server.docCalls()).toHaveLength(1)
    await waitFor(() => expect(heading('Ondrey')).toHaveFocus())
    expect(live.s.doc.kind).toBe('open')
  })

  it('a different document replaces a clean one with no dialog (CANVAS-6)', async () => {
    const { server } = await mountSelected()
    await run(() => open('doc_a'))
    await run(() => open('doc_b', true, 'Brannoch'))
    expect(live.s.doc.kind).toBe('open')
    expect(heading('Brannoch')).toBeInTheDocument()
    expect(heading('Ondrey')).toBeNull()
    expect(live.s.guardDialog).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_b')
    expect(server.docCalls()).toHaveLength(2)
  })

  it('an answer that arrives after a second open is dropped (T-5, stale sequence)', async () => {
    const { server } = await mountSelected({
      route: (call) => (/documents/.test(call.url) ? 'defer' : defaultRoute(call)),
    })
    act(() => { void open('doc_a') })
    act(() => { void open('doc_b') })
    expect(server.docCalls()).toHaveLength(2)
    act(() => server.docCalls()[1].reply({ status: 200, body: docBody('cmp_A', 'doc_b') }))
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    act(() => server.docCalls()[0].reply({ status: 200, body: docBody('cmp_A', 'doc_a') }))
    await flush()
    expect(heading('Brannoch')).toBeInTheDocument()
    expect(heading('Ondrey')).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_b')
  })

  it('moves focus only if it has not moved since the gesture (C-4): a GM typing keeps the composer', async () => {
    const { server } = await mountSelected({
      route: (call) => (/documents/.test(call.url) ? 'defer' : defaultRoute(call)),
    })
    const opener = document.getElementById('opener') as HTMLElement
    opener.focus()
    act(() => { void open('doc_a') })
    const composer = screen.getByLabelText('composer')
    composer.focus()
    act(() => server.docCalls()[0].reply({ status: 200, body: docBody('cmp_A', 'doc_a') }))
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    await flush()
    expect(composer).toHaveFocus()
  })

  it('a deep-link restore opens after the campaign is selected, moves no focus, and shows the canvas view', async () => {
    const { server } = await mount({
      hash: '#campaign=cmp_A&document=doc_a',
      restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
      route: (call) => (call.url === '/campaigns/cmp_A' ? 'defer' : defaultRoute(call)),
    })
    const composer = screen.getByLabelText('composer')
    composer.focus()
    await flush()
    // The server's say on the campaign comes first (I-14): no document request yet.
    expect(server.docCalls()).toHaveLength(0)
    expect(live.s.doc.kind).toBe('closed')
    act(() => server.calls[0].reply({ status: 200, body: campaign('cmp_A') }))
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    expect(live.s.view).toBe('canvas')
    expect(composer).toHaveFocus()
    expect(server.docCalls()).toHaveLength(1)
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
  })

  it('a restore with nothing focused leaves focus alone: only a gesture moves it', async () => {
    await mount({
      hash: '#campaign=cmp_A&document=doc_a',
      restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
    })
    expect(document.activeElement).toBe(document.body)
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    await flush()
    expect(document.activeElement).toBe(document.body)
    expect(heading('Ondrey')).not.toHaveFocus()
  })

  it('a restore under StrictMode makes exactly one document request (C-13)', async () => {
    const { server } = await mount({
      strict: true,
      hash: '#campaign=cmp_A&document=doc_a',
      restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
    })
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    await flush()
    expect(server.docCalls()).toHaveLength(1)
  })

  it('a restore waits for the Workbench to be active: no document fetch in another channel (X-9)', async () => {
    const { server } = await mount({
      mode: 'sage',
      hash: '#campaign=cmp_A&document=doc_a',
      restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
    })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await flush()
    expect(server.docCalls()).toHaveLength(0)
    act(() => live.nav.setMode('gm'))
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    expect(server.docCalls()).toHaveLength(1)
  })

  it.each([
    ['a missing document', 'doc_missing', 'unavailable'],
    ['a document from a newer version', 'doc_newer', 'unsupported'],
  ])('%s is its own state', async (_label, id, kind) => {
    await mountSelected()
    await run(() => open(id))
    expect(live.s.doc.kind).toBe(kind)
    await waitFor(() => expect(screen.getByRole('heading', { name: kind })).toHaveFocus())
  })

  it('a failed read is failed, keeps its title, and Retry asks again', async () => {
    let answers: Reply[] = ['network', { status: 503, body: {} }]
    const { server } = await mountSelected({
      route: (call) => (/documents/.test(call.url) ? (answers.shift() ?? defaultRoute(call)) : defaultRoute(call)),
    })
    await run(() => open('doc_a', true, 'Ondrey'))
    expect(live.s.doc).toEqual({ kind: 'failed', documentId: 'doc_a', title: 'Ondrey' })
    act(() => live.a.retry())
    await waitFor(() => expect(live.s.doc.kind).toBe('failed'))
    answers = []
    act(() => live.a.retry())
    await waitFor(() => expect(live.s.doc.kind).toBe('open'))
    expect(server.docCalls()).toHaveLength(3)
  })

  it('an aborted request is a failed read, never a rejection (C-17)', async () => {
    const unhandled = vi.fn()
    window.addEventListener('unhandledrejection', unhandled)
    const { server } = await mountSelected({
      route: (call) => (/documents/.test(call.url) ? 'abort' : defaultRoute(call)),
    })
    await expect(run(() => open('doc_a', true, 'Ondrey'))).resolves.toBeUndefined()
    await expect(run(() => live.a.closeDocument())).resolves.toBe(true)
    await expect(run(() => open('doc_b'))).resolves.toBeUndefined()
    expect(live.s.doc.kind).toBe('failed')
    expect(server.docCalls()).toHaveLength(2)
    window.removeEventListener('unhandledrejection', unhandled)
    expect(unhandled).not.toHaveBeenCalled()
  })

  it('every open and close is a GET and nothing else: no PATCH, no tool or chat POST (C-12c)', async () => {
    const { server } = await mountSelected()
    await run(() => open('doc_a'))
    await run(() => open('doc_b'))
    await run(() => live.a.closeDocument())
    expect(server.calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('a malformed id makes no request and reads as unavailable', async () => {
    const { server } = await mountSelected()
    await run(() => open('Ondrey the Wise'))
    expect(server.docCalls()).toHaveLength(0)
    expect(live.s.doc.kind).toBe('unavailable')
  })
})

describe('closing and the view (T-5, C-5)', () => {
  it('closing clears the fragment key, returns to the chat view and closes the document', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    expect(live.s.view).toBe('canvas')
    expect(await run(() => live.a.closeDocument())).toBe(true)
    expect(live.s.doc).toEqual({ kind: 'closed' })
    expect(live.s.view).toBe('chat')
    expect(live.d.documentKey).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A')
  })

  it('closing applies its state before it resolves, so the caller can focus what just re-appeared (C-3b)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    let shownAtResolve = ''
    await run(async () => {
      await live.a.closeDocument()
      shownAtResolve = stateText()
    })
    expect(shownAtResolve).toBe('closed|chat|false')
  })

  it('closing while an open is still in flight drops its answer', async () => {
    const { server } = await mountSelected({
      route: (call) => (/documents/.test(call.url) ? 'defer' : defaultRoute(call)),
    })
    act(() => { void open('doc_a') })
    expect(await run(() => live.a.closeDocument())).toBe(true)
    act(() => server.docCalls()[0].reply({ status: 200, body: docBody('cmp_A', 'doc_a') }))
    await flush()
    expect(live.s.doc.kind).toBe('closed')
    expect(heading('Ondrey')).toBeNull()
  })

  it('opening by a gesture sets the canvas view whatever the layout, and setView switches it (C-5)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    expect(live.s.view).toBe('canvas')
    act(() => live.a.setView('chat'))
    expect(live.s.view).toBe('chat')
    expect(live.s.doc.kind).toBe('open')
    await run(() => open('doc_b'))
    expect(live.s.view).toBe('canvas')
  })

  it('the Chat view is never lost by a same-document open, which shows the canvas again', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    act(() => live.a.setView('chat'))
    await run(() => open('doc_a'))
    expect(live.s.view).toBe('canvas')
  })
})

describe('hidden, not closed (CANVAS-9)', () => {
  it('a document stays open through Sage and returns with the GM channel (AE-73)', async () => {
    const { server } = await mountSelected()
    await run(() => open('doc_a'))
    act(() => live.nav.setMode('sage'))
    expect(live.active).toBe(false)
    expect(live.s.doc.kind).toBe('open')
    expect(canvasShown(live.active, live.s.doc)).toBe(false)
    expect(window.location.hash).toBe('')
    act(() => live.nav.setMode('gm'))
    expect(live.s.doc.kind).toBe('open')
    expect(canvasShown(live.active, live.s.doc)).toBe(true)
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a'))
    expect(server.docCalls()).toHaveLength(1)
  })
})

// ── Scope and identity (LIB-25, C-12) ─────────────────────────────────────────

describe('a campaign or account change drops the canvas (C-12, LIB-25)', () => {
  it('a campaign switch closes the canvas, and a late answer for the old campaign is ignored', async () => {
    const { server } = await mountSelected({
      route: (call) => (/documents/.test(call.url) ? 'defer' : defaultRoute(call)),
    })
    act(() => { void open('doc_a') })
    await run(() => live.c.selectCampaign(campaign('cmp_B')))
    expect(live.s.doc).toEqual({ kind: 'closed' })
    act(() => server.docCalls()[0].reply({ status: 200, body: docBody('cmp_A', 'doc_a') }))
    await flush()
    expect(live.s.doc.kind).toBe('closed')
    expect(heading('Ondrey')).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_B')
  })

  it('an open document is gone from state and DOM the moment the campaign changes', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    expect(heading('Ondrey')).toBeInTheDocument()
    await run(() => live.c.selectCampaign(campaign('cmp_B')))
    expect(heading('Ondrey')).toBeNull()
    // No render ever committed campaign A's document under campaign B (the render of the switch included).
    expect(rendered.filter((r) => r.shows !== null && r.shows !== r.selected)).toEqual([])
    expect(live.s.view).toBe('chat')
    expect(live.a.openerRef.current).toBeNull()
  })

  it('A -> B -> A starts closed each time', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    await run(() => live.c.selectCampaign(campaign('cmp_B')))
    await run(() => live.c.selectCampaign(campaign('cmp_A')))
    expect(live.s.doc).toEqual({ kind: 'closed' })
  })

  it('a role change keeps the shell mounted but drops the document: no GM text stays in memory (C-12a)', async () => {
    const { server, signal } = await mountSelected()
    await run(() => open('doc_a'))
    expect(heading('Ondrey')).toBeInTheDocument()
    const before = server.docCalls().length
    vi.mocked(api.getMe).mockResolvedValue({ kind: 'ok', user: { email: ADA, role: 'player' } })
    await signal.receive({ v: 1, kind: 'identity-changed' })
    await waitFor(() => expect(live.user.user.role).toBe('player'))
    expect(live.s.doc).toEqual({ kind: 'closed' })
    expect(heading('Ondrey')).toBeNull()
    expect(screen.queryByText(/Ondrey/)).toBeNull()
    expect(server.docCalls()).toHaveLength(before)
  })

  it("another dm's link makes no document request at all (C-12b, I-14)", async () => {
    const { server } = await mount({
      hash: '#campaign=cmp_A&document=doc_a',
      restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
      route: (call) => (call.url === '/campaigns/cmp_A' ? { status: 404, body: {} } : defaultRoute(call)),
    })
    await waitFor(() => expect(live.c.selection.kind).toBe('unavailable'))
    await flush()
    expect(server.docCalls()).toHaveLength(0)
    expect(live.s.doc.kind).toBe('closed')
  })

  it('a player makes no request even with a document key in the URL (C-12d)', async () => {
    const { server } = await mount({
      role: 'player',
      hash: '#campaign=cmp_A&document=doc_a',
      restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
    })
    await flush()
    expect(server.calls).toHaveLength(0)
    expect(live.s.doc.kind).toBe('closed')
  })
})

// ── A fragment edit outside the app ───────────────────────────────────────────

describe('a hash link to another document (CANVAS-30)', () => {
  function edit(hash: string): void {
    window.history.replaceState(null, '', `/workspace${hash}`)
    act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
  }

  it('replaces a clean document by GET and then shows the new id in the URL', async () => {
    const { server } = await mountSelected()
    await run(() => open('doc_a'))
    edit('#campaign=cmp_A&document=doc_b')
    await waitFor(() => expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_b'))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_b'))
    expect(server.calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('a link to the document already shown changes nothing and fetches nothing', async () => {
    const { server } = await mountSelected()
    await run(() => open('doc_a'))
    edit('#campaign=cmp_A&document=doc_a')
    await flush()
    expect(server.docCalls()).toHaveLength(1)
  })

  it('is ignored outside the GM channel, where there is no Workbench', async () => {
    const { server } = await mountSelected()
    act(() => live.nav.setMode('sage'))
    edit('#campaign=cmp_A&document=doc_b')
    await flush()
    expect(server.docCalls()).toHaveLength(0)
  })
})

// ── No implicit swap or close (T-6, CANVAS-3/4/18) ────────────────────────────

describe('chat never swaps or closes the document (T-6)', () => {
  it('a conversation switch and a settled history load leave the document and the network alone', async () => {
    const { server } = await mountSelected()
    await run(() => open('doc_a'))
    const requests = server.lines().length
    act(() => live.nav.setConversationId('cnv_other'))
    act(() => live.nav.setConversationId(null))
    await flush()
    expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_a')
    expect(heading('Ondrey')).toBeInTheDocument()
    expect(server.lines()).toHaveLength(requests)
    expect(live.s.view).toBe('canvas')
  })

  it('opens the document only on a gesture: nothing but openDocument ever fetches one', async () => {
    const { server } = await mountSelected()
    await flush()
    expect(server.docCalls()).toHaveLength(0)
    await run(() => open('doc_a'))
    expect(server.docCalls()).toHaveLength(1)
  })
})

// ── The loss guard (T-8, CANVAS-16/17) ────────────────────────────────────────

describe('the loss guard around the canvas (T-8)', () => {
  it('closing a clean canvas, or one whose flush saved, proceeds with no dialog (AE-18)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    act(() => { live.a.registerDirtySource(source) })
    expect(await run(() => live.a.closeDocument())).toBe(true)
    expect(source.flushes).toBe(1)
    expect(live.s.guardDialog).toBeNull()
    expect(live.s.doc.kind).toBe('closed')
  })

  it('a replace fetches the target FIRST while the current document stays', async () => {
    const { server } = await mountSelected({
      route: (call) => (/doc_b/.test(call.url) ? 'defer' : defaultRoute(call)),
    })
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    act(() => { void open('doc_b', true, 'Brannoch') })
    await flush()
    // The target is in flight; the guard has not run, the current document and URL are untouched.
    expect(server.docCalls()).toHaveLength(2)
    expect(source.flushes).toBe(0)
    expect(live.s.guardDialog).toBeNull()
    expect(live.s.doc.kind).toBe('open')
    expect(heading('Ondrey')).toBeInTheDocument()
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
    act(() => server.docCalls()[1].reply({ status: 200, body: docBody('cmp_A', 'doc_b') }))
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    // The dialog came AFTER the target loaded, and the swap waits for the GM's answer.
    expect(source.flushes).toBe(1)
    expect(heading('Ondrey')).toBeInTheDocument()
    expect(heading('Brannoch')).toBeNull()
  })

  it('Keep editing refuses the swap: the current document and URL stay, nothing is discarded (AE-47)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    act(() => { void open('doc_b') })
    await waitFor(() => expect(live.s.guardDialog).toEqual({ title: 'Ondrey', retryable: false, busy: false }))
    act(() => live.a.guard.keep())
    await flush()
    expect(live.s.guardDialog).toBeNull()
    expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_a')
    expect(source.discards).toBe(0)
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
  })

  it('Discard changes discards, then swaps and writes the new id', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    act(() => { void open('doc_b') })
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    act(() => live.a.guard.discard())
    await waitFor(() => expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_b'))
    expect(source.discards).toBe(1)
    expect(live.s.guardDialog).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_b')
  })

  it('Try saving again proceeds when the second flush saves, and stays open when it does not', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'retryable'
    act(() => { live.a.registerDirtySource(source) })
    act(() => { void open('doc_b') })
    await waitFor(() => expect(live.s.guardDialog?.retryable).toBe(true))
    await run(async () => live.a.guard.retry())
    expect(live.s.guardDialog).toEqual({ title: 'Ondrey', retryable: true, busy: false })
    expect(source.flushes).toBe(2)
    source.outcome = 'saved'
    await run(async () => live.a.guard.retry())
    await waitFor(() => expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_b'))
    expect(live.s.guardDialog).toBeNull()
    expect(source.discards).toBe(0)
  })

  it('a target that cannot load changes nothing: no dialog, no flush, and it says so (CANVAS-16)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    await run(() => open('doc_missing', true, 'Vanished'))
    expect(live.s.guardDialog).toBeNull()
    expect(source.flushes).toBe(0)
    expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_a')
    expect(live.s.announcement).toBe("Couldn't open Vanished. Nothing changed.")
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
  })

  it('offline is refused with no dialog and the offline message (AE-19)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'offline'
    act(() => { live.a.registerDirtySource(source) })
    expect(await run(() => live.a.closeDocument())).toBe(false)
    expect(live.s.guardDialog).toBeNull()
    expect(live.s.announcement).toBe("You're offline — changes aren't saved. Keep this tab open.")
    expect(live.s.doc.kind).toBe('open')
    await run(() => open('doc_b'))
    expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_a')
    expect(source.discards).toBe(0)
  })

  it('closing a dirty canvas asks: Keep editing leaves it open and returns false', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    let result: boolean | null = null
    act(() => { void live.a.closeDocument().then((r) => { result = r }) })
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    act(() => live.a.guard.keep())
    await waitFor(() => expect(result).toBe(false))
    expect(live.s.doc.kind).toBe('open')
  })

  it('a campaign switch runs the guard: Keep editing vetoes it, Discard lets it through (LIB-25)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    let outcome = ''
    act(() => { void live.c.selectCampaign(campaign('cmp_B')).then((o) => { outcome = o }) })
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    act(() => live.a.guard.keep())
    await waitFor(() => expect(outcome).toBe('vetoed'))
    expect(live.c.selection.kind === 'selected' && live.c.selection.campaign.campaign_id).toBe('cmp_A')
    expect(live.s.doc.kind).toBe('open')
    act(() => { void live.c.selectCampaign(campaign('cmp_B')).then((o) => { outcome = o }) })
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    act(() => live.a.guard.discard())
    await waitFor(() => expect(outcome).toBe('switched'))
    expect(source.discards).toBe(1)
    expect(live.s.doc.kind).toBe('closed')
  })

  it('a hash link to another document runs the guard too, and Keep editing restores the fragment (CANVAS-30)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    window.history.replaceState(null, '', '/workspace#campaign=cmp_A&document=doc_b')
    act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
    act(() => live.a.guard.keep())
    await flush()
    expect(live.s.doc.kind === 'open' && live.s.doc.document.document_id).toBe('doc_a')
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
  })

  it('a second guard while a dialog is open is refused instead of stacking a second dialog', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    act(() => { void live.a.closeDocument() })
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    expect(await run(() => live.a.closeDocument())).toBe(false)
    act(() => live.a.guard.keep())
    await flush()
    expect(live.s.guardDialog).toBeNull()
  })

  it('a flush that never answers is cut off at the timeout and asks (§5.3)', async () => {
    await mountSelected()
    await run(() => open('doc_a'))
    vi.useFakeTimers()
    const hung: CanvasDirtySource = {
      isDirty: () => true,
      flush: () => new Promise<FlushOutcome>(() => {}),
      discard: () => {},
    }
    act(() => { live.a.registerDirtySource(hung) })
    act(() => { void live.a.closeDocument() })
    await act(async () => { await vi.advanceTimersByTimeAsync(FLUSH_TIMEOUT_MS) })
    expect(live.s.guardDialog?.retryable).toBe(true)
  })

  it('an identity change while the dialog is open closes it and resolves the guard as refused', async () => {
    const { signal } = await mountSelected()
    await run(() => open('doc_a'))
    const source = new TestSource()
    source.outcome = 'failed'
    act(() => { live.a.registerDirtySource(source) })
    let result: boolean | null = null
    act(() => { void live.a.closeDocument().then((r) => { result = r }) })
    await waitFor(() => expect(live.s.guardDialog).not.toBeNull())
    vi.mocked(api.getMe).mockResolvedValue({ kind: 'ok', user: { email: ADA, role: 'player' } })
    await signal.receive({ v: 1, kind: 'identity-changed' })
    await waitFor(() => expect(live.s.guardDialog).toBeNull())
    expect(result).toBe(false)
    expect(source.discards).toBe(0)
  })
})

describe('beforeunload (CANVAS-17)', () => {
  function fire(): boolean {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  }

  it('is prevented only while a registered source is dirty, and never once unregistered', async () => {
    await mountSelected()
    expect(fire()).toBe(false)
    const source = new TestSource()
    let off = (): void => {}
    act(() => { off = live.a.registerDirtySource(source) })
    expect(fire()).toBe(true)
    source.dirty = false
    expect(fire()).toBe(false)
    source.dirty = true
    act(() => off())
    expect(fire()).toBe(false)
  })

  it('keeps one listener for the provider’s life, and removes it on unmount', async () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const { view } = await mountSelected()
    const source = new TestSource()
    act(() => { live.a.registerDirtySource(source) })
    expect(fire()).toBe(true)
    expect(add.mock.calls.filter(([type]) => type === 'beforeunload')).toHaveLength(1)
    view.unmount()
    expect(remove.mock.calls.filter(([type]) => type === 'beforeunload')).toHaveLength(1)
    expect(fire()).toBe(false)
  })
})

// ── Render cost (C-13) ────────────────────────────────────────────────────────

describe('render cost (C-13)', () => {
  it('a consumer of the stable actions is not re-rendered by loading, opening or closing a document', async () => {
    const renders = vi.fn()
    function ChatLike(): React.JSX.Element {
      useCanvasActions()
      useCampaign()
      return <p>chat</p>
    }
    const server = stubServer()
    const signal = channels()
    window.history.replaceState(null, '', '/workspace#campaign=cmp_A')
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: ADA, role: 'dm' } })
    render(
      <AppNavProvider initialScreen="workspace" initialMode="gm">
        <CurrentUserProvider identityChannelFactory={signal.factory}>
          <CampaignProvider fetchImpl={server.fetchImpl} restore={{ campaignId: 'cmp_A', conversationId: null }}>
            <CanvasProvider fetchImpl={server.fetchImpl}>
              <React.Profiler id="chat" onRender={renders}><ChatLike /></React.Profiler>
              <Surface />
            </CanvasProvider>
          </CampaignProvider>
        </CurrentUserProvider>
      </AppNavProvider>,
    )
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await flush()
    const settled = renders.mock.calls.length
    await run(() => open('doc_a'))
    await run(() => open('doc_b'))
    expect(live.s.doc.kind).toBe('open')
    await run(() => live.a.closeDocument())
    expect(live.s.doc.kind).toBe('closed')
    expect(renders.mock.calls.length).toBe(settled)
  })
})
