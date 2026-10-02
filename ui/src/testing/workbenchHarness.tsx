/**
 * workbenchHarness -- the real providers around a Workbench surface, with a
 * recording server that can hold an answer back (agent-forge-harness-1kg.6.3).
 *
 * What the Workbench tests share: the REAL AppNav, CurrentUser, Campaign and Canvas
 * providers (so scope keys, the fragment, focus and the loss guard behave as they
 * do in the app), a `fetch` stub whose every call is recorded and whose answers
 * can be `defer`red so each race really interleaves, and a few contract-valid
 * fixtures (a campaign, an NPC document, a version history, a library page).
 *
 * Nothing here is imported by production code.
 */

/* eslint-disable react-refresh/only-export-components -- a test harness: never hot-reloaded, and its probe is not a screen */
import * as React from 'react'
import { act, render, waitFor } from '@testing-library/react'
import { expect, vi } from 'vitest'
import * as api from '../api'
import { CampaignSchema, type Campaign } from '../gm/contracts'
import { DOCUMENT_FIXTURES } from '../gm/documentFixtures'
import { AppNavProvider, useAppNav, type AppNavState, type ChatMode, type Screen } from '../shell/AppNav'
import {
  CampaignProvider, useCampaign, useCampaignDocument, type CampaignContextValue, type CampaignDocumentValue,
} from '../shell/campaignContext'
import {
  CanvasProvider, useCanvasActions, useCanvasState, useWorkbenchActive, type CanvasActions, type CanvasState,
} from '../shell/canvasContext'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from '../shell/currentUser'
import type { IdentityChannelLike } from '../shell/identityBroadcast'
import type { CampaignRestore } from '../shell/workspaceFragment'

// ── Fixtures ─────────────────────────────────────────────────────────────────

export const GM_EMAIL = 'ada@example.com'

export function campaignFixture(id: string, name = `Name of ${id}`): Campaign {
  return CampaignSchema.parse({
    schema_version: 1, campaign_id: id, name, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
    avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
  })
}

/** Document names by id; any other id is its own name. */
export const DOC_NAMES: Readonly<Record<string, string>> = {
  doc_a: 'Ondrey', doc_b: 'Brannoch', doc_c: 'Cressa', doc_d: 'Dunmere',
}

export function documentBody(campaignId: string, id: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  const base = DOCUMENT_FIXTURES.npc
  return {
    ...base, document_id: id, campaign_id: campaignId,
    data: { ...(base.data as Record<string, unknown>), name: DOC_NAMES[id] ?? id },
    ...extra,
  }
}

export function versionBody(number: number, summary = `Edit ${number}`): Record<string, unknown> {
  return {
    number, author: number % 2 === 0 ? 'gm' : 'assistant', summary, created_at: '2026-09-16T19:36:00Z',
    sealed: true, changed_fields: ['wants'], restored_from: null,
  }
}

export function historyBody(documentId: string, numbers: readonly number[], nextCursor: string | null = null): Record<string, unknown> {
  return {
    schema_version: 1, document_id: documentId, items: numbers.map((n) => versionBody(n)), next_cursor: nextCursor,
  }
}

export function libraryBody(
  campaignId: string,
  category: string,
  items: ReadonlyArray<{ id: string; type: string; title: string; updatedAt?: string }> = [],
): Record<string, unknown> {
  return {
    schema_version: 1, campaign_id: campaignId, category,
    items: items.map((item) => ({
      document_id: item.id, type: item.type, title: item.title, qualifier: '', tags: [], archived: false,
      updated_at: item.updatedAt ?? '2026-09-16T19:36:00Z',
    })),
    next_cursor: null,
  }
}

// ── The recording server ─────────────────────────────────────────────────────

export type Reply = { status: number; body?: unknown; raw?: string } | 'network' | 'abort'
export interface Call {
  url: string
  method: string
  body: string | null
  reply: (reply: Reply) => void
}
export type Route = (call: Call) => Reply | 'defer'

/** Campaign by id, a document by id (`doc_missing` is a 404, `doc_newer` a newer schema), its history, and nothing else. */
export const defaultWorkbenchRoute: Route = ({ url }) => {
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (one !== null) return { status: 200, body: campaignFixture(one[1]) }
  const doc = /^\/campaigns\/(cmp_\w+)\/documents\/(doc_\w+)$/.exec(url)
  if (doc !== null) {
    if (doc[2] === 'doc_missing') return { status: 404, body: { code: 'not_found' } }
    if (doc[2] === 'doc_newer') return { status: 200, body: documentBody(doc[1], doc[2], { schema_version: 2 }) }
    return { status: 200, body: documentBody(doc[1], doc[2]) }
  }
  const versions = /^\/campaigns\/(cmp_\w+)\/documents\/(doc_\w+)\/versions/.exec(url)
  if (versions !== null) return { status: 200, body: historyBody(versions[2], [3, 2, 1]) }
  return { status: 404, body: {} }
}

export function stubServer(route: Route = defaultWorkbenchRoute) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null,
      reply: (reply) => {
        if (reply === 'network') reject(new TypeError('Failed to fetch'))
        else if (reply === 'abort') reject(new DOMException('aborted', 'AbortError'))
        else resolve(new Response(reply.raw ?? JSON.stringify(reply.body ?? {}), { status: reply.status }))
      },
    }
    calls.push(call)
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  return {
    fetchImpl,
    calls,
    lines: () => calls.map((call) => `${call.method} ${call.url}`),
    docCalls: () => calls.filter((call) => /\/documents\/[^/]+$/.test(call.url)),
    historyCalls: () => calls.filter((call) => /\/versions/.test(call.url)),
    libraryCalls: () => calls.filter((call) => /\/library$/.test(call.url)),
  }
}
export type StubServer = ReturnType<typeof stubServer>

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
    for (const channel of opened) channel.onmessage?.(new MessageEvent('message', { data }))
  })
  return { factory, receive }
}

// ── The mount ────────────────────────────────────────────────────────────────

/** What every mounted Workbench exposes to a test, refreshed on each committed render. */
export const live = {} as {
  campaign: CampaignContextValue; document: CampaignDocumentValue; nav: AppNavState; user: CurrentUserContextValue
  state: CanvasState; actions: CanvasActions; active: boolean
}

function WorkbenchProbe(): null {
  const state = useCanvasState()
  const actions = useCanvasActions()
  const campaign = useCampaign()
  const document = useCampaignDocument()
  const nav = useAppNav()
  const user = useCurrentUser()
  const active = useWorkbenchActive()
  React.useLayoutEffect(() => {
    live.state = state
    live.actions = actions
    live.campaign = campaign
    live.document = document
    live.nav = nav
    live.user = user
    live.active = active
  })
  return null
}

export interface MountOptions {
  role?: 'dm' | 'player'
  restore?: CampaignRestore
  route?: Route
  hash?: string
  mode?: ChatMode
  screen?: Screen
  strict?: boolean
  /** Extra providers between the canvas and the surface (a model catalog, say). */
  wrap?: (children: React.ReactNode) => React.ReactNode
}

/** Signed in as a dm (or `role`), at `/workspace`, with the real providers. `ui` receives the server so a surface can be handed its `fetchImpl`. */
export async function mountWorkbench(ui: (server: StubServer) => React.ReactElement, options: MountOptions = {}) {
  const server = stubServer(options.route)
  const signal = channels()
  window.history.replaceState(null, '', `/workspace${options.hash ?? ''}`)
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: GM_EMAIL, role: options.role ?? 'dm' } })
  const wrap = options.wrap ?? ((children: React.ReactNode) => children)
  const tree = (
    <AppNavProvider initialScreen={options.screen ?? 'workspace'} initialMode={options.mode ?? 'gm'}>
      <CurrentUserProvider identityChannelFactory={signal.factory}>
        <CampaignProvider fetchImpl={server.fetchImpl} restore={options.restore ?? null}>
          <CanvasProvider fetchImpl={server.fetchImpl}>
            <WorkbenchProbe />
            {wrap(ui(server))}
          </CanvasProvider>
        </CampaignProvider>
      </CurrentUserProvider>
    </AppNavProvider>
  )
  const view = render(options.strict === true ? <React.StrictMode>{tree}</React.StrictMode> : tree)
  await waitFor(() => expect(live.user.authStatus).not.toBe('checking'))
  return { server, signal, view }
}

/** A dm with campaign `cmp_A` selected and the canvas closed. */
export async function mountSelected(ui: (server: StubServer) => React.ReactElement, options: MountOptions = {}) {
  const mounted = await mountWorkbench(ui, {
    hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null }, ...options,
  })
  await waitFor(() => expect(live.campaign.selection.kind).toBe('selected'))
  return mounted
}

export const flush = (): Promise<void> => act(async () => {})
export const run = <T,>(fn: () => Promise<T>): Promise<T> => act(fn)
