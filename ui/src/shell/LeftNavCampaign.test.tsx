/**
 * LeftNavCampaign.test.tsx -- LeftNav's campaign branch (agent-forge-harness-1kg.2.5,
 * PR-2; brief sections 7.7 and 9 T2-4..T2-7, T2-11, T2-13 and the Critic's
 * items 5, 17 and 22).
 *
 * LeftNav is mounted under the REAL AppNav, CurrentUser and campaign providers
 * with `fetch` replaced by a recorder that can hold an answer back and, like a
 * browser's, rejects with an AbortError once its signal aborts (pr178-mpost
 * N-2). The legacy-markup snapshot (T2-6) was recorded against LeftNav as it was BEFORE
 * this PR touched it, and its snapshot file changes only by 74j's GM entry button
 * (74j brief, Addendum 2 R-4).
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { CampaignSchema } from '../gm/contracts'
import { AppNavProvider, useAppNav, type AppNavState } from './AppNav'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
import { CampaignThreadsContext } from './campaignThreads'
import type { IdentityChannelLike } from './identityBroadcast'
import { LeftNav } from './LeftNav'

// ── Harness ────────────────────────────────────────────────────────────────────

function campaignBody(id: string, over: Record<string, unknown> = {}) {
  return {
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false, ...over,
  }
}

type Reply = { status: number; body?: unknown } | 'network'
interface Call { url: string; method: string; body: string | null; signal: AbortSignal | null; reply: (r: Reply) => void }
type Route = (call: Call) => Reply | 'defer'

const defaultRoute: Route = ({ url }) => {
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (one !== null) return { status: 200, body: campaignBody(one[1]) }
  return { status: 404, body: {} }
}

function stubServer(route: Route) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null,
      signal: init?.signal ?? null,
      reply: (r) => (r === 'network'
        ? reject(new TypeError('Failed to fetch'))
        : resolve(new Response(JSON.stringify(r.body ?? {}), { status: r.status }))),
    }
    calls.push(call)
    call.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')))
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  return { fetchImpl, calls, lines: () => calls.map((c) => `${c.method} ${c.url}`) }
}

const live = {} as { c: CampaignContextValue; nav: AppNavState; user: CurrentUserContextValue; lists: () => number; onCommit?: (() => void) | undefined }
/** Re-renders on every campaign-context and thread-store change, and its layout effect runs after the DOM of the same commit is written: P11 reads each commit through `live.onCommit`. */
function Probe(): null {
  const c = useCampaign()
  const nav = useAppNav()
  const user = useCurrentUser()
  const threads = React.useContext(CampaignThreadsContext)
  React.useSyncExternalStore(threads?.store.subscribe ?? (() => () => {}), threads?.store.getSnapshot ?? (() => null))
  React.useLayoutEffect(() => {
    live.c = c
    live.nav = nav
    live.user = user
    live.lists = () => threads?.store.getSnapshot().size ?? -1
    live.onCommit?.()
  })
  return null
}

interface MountOptions { campaign?: string | null; route?: Route; onNavigate?: () => void }
async function mount({ campaign = 'cmp_A', route = defaultRoute, onNavigate }: MountOptions = {}) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  window.history.replaceState(null, '', '/workspace')
  const server = stubServer(route)
  const channels: IdentityChannelLike[] = []
  const store = new MemoryConversationStore()
  store.create('gm', 'A legacy GM question about the heist')
  store.create('gm', 'Another legacy GM question')
  store.create('sage', 'A Sage question about grappling')
  store.create('spell', 'Fireball at higher levels')
  store.create('rules', 'How does cover work')
  const view = render(
    <AppNavProvider initialScreen="workspace" initialMode="gm">
      <CurrentUserProvider identityChannelFactory={() => {
        const channel: IdentityChannelLike = { postMessage: () => {}, close: () => {}, onmessage: null }
        channels.push(channel)
        return channel
      }}>
        <ConversationStoreProvider store={store}>
          <CampaignProvider
            fetchImpl={server.fetchImpl}
            restore={campaign === null ? null : { campaignId: campaign, conversationId: null }}
          >
            <LeftNav onNavigate={onNavigate} />
            <Probe />
          </CampaignProvider>
        </ConversationStoreProvider>
      </CurrentUserProvider>
    </AppNavProvider>,
  )
  await waitFor(() => expect(live.c.enabled).toBe(true))
  /** Another tab signed in as `email`: this tab's background re-check sees it. */
  const switchTo = async (email: string) => {
    vi.mocked(api.getMe).mockResolvedValue({ kind: 'ok', user: { email, role: 'dm' } })
    act(() => {
      for (const c of channels) c.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    })
    await waitFor(() => expect(live.user.user.id).toBe(email))
  }
  return { server, store, view, switchTo }
}

/** What a user of assistive technology meets, in document order. */
function roleTree(): string {
  const nav = screen.getByRole('navigation', { name: 'Main navigation' })
  const implicit: Record<string, string> = { NAV: 'navigation', P: 'paragraph', BUTTON: 'button', INPUT: 'textbox' }
  return Array.from(nav.querySelectorAll<HTMLElement>('nav, p, button, input, [role]'))
    .filter((el) => el.closest('[aria-hidden="true"]') === null)
    .map((el) => {
      const flags = ['aria-pressed', 'aria-disabled', 'disabled', 'aria-invalid']
        .filter((name) => el.hasAttribute(name))
        .map((name) => `${name}=${el.getAttribute(name) ?? ''}`)
      const name = el.getAttribute('aria-label') ?? (el.textContent ?? '').trim()
      return [el.getAttribute('role') ?? implicit[el.tagName], JSON.stringify(name), ...flags].join(' ')
    })
    .join('\n')
}

/** Brief section 11 (review pr212 M-1): LeftNav's campaign states are visible
 * text, never a live region; the pane's one announcer is ChatPane's. */
function expectNoLiveRegion(): void {
  const nav = screen.getByRole('navigation', { name: 'Main navigation' })
  expect(nav.querySelectorAll('[role="status"], [role="alert"], [role="log"], [aria-live]')).toHaveLength(0)
}

/** Counts added nodes (or changed text) holding `text`, read from the records
 * themselves: a row added and removed inside one act() still counts. */
function watchAdded(text: string): () => number {
  let hits = 0
  const scan = (records: MutationRecord[]) => records.forEach((record) => {
    const nodes = record.type === 'characterData' ? [record.target] : Array.from(record.addedNodes)
    if (nodes.some((node) => node.textContent?.includes(text) === true)) hits += 1
  })
  const observer = new MutationObserver(scan)
  observer.observe(document.body, { subtree: true, childList: true, characterData: true })
  return () => { scan(observer.takeRecords()); observer.disconnect(); return hits }
}

afterEach(() => {
  live.onCommit = undefined
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

// ── T2-6: the legacy sidebar is untouched ─────────────────────────────────────

describe('legacy markup (T2-6)', () => {
  it('GM with no campaign renders the sidebar exactly as before this PR', async () => {
    await mount({ campaign: null })
    expect(live.c.selection.kind).toBe('none')
    expect(roleTree()).toMatchSnapshot()
  })

  it('every other channel renders the sidebar exactly as before this PR, a campaign selected or not', async () => {
    await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    for (const channel of ['Sage', 'Spell', 'Rules']) {
      await userEvent.click(screen.getByRole('button', { name: channel }))
      expect(live.nav.mode).toBe(channel.toLowerCase())
      expect(live.c.selection.kind).toBe('selected')
      expect(roleTree()).toMatchSnapshot(channel)
    }
    await act(async () => { await live.c.clearCampaign() })
    await userEvent.click(screen.getByRole('button', { name: 'GM' }))
    expect(roleTree()).toMatchSnapshot('GM after a clear')
  })
})

// ── The campaign branch ───────────────────────────────────────────────────────

const threadBody = (id: string, over: Record<string, unknown> = {}) => ({
  schema_version: 1, conversation_id: id, campaign_id: 'cmp_A', title: `Thread ${id}`, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null, ...over,
})
const threadPage = (items: unknown[], next: string | null = null) => ({ schema_version: 1, items, next_cursor: next })

/** Campaigns by id; each campaign's list holds `<id>-1` and `<id>-2`; `overrides` answers first. */
function serve(overrides: (call: Call) => Reply | 'defer' | undefined = () => undefined): Route {
  return (call) => {
    const override = overrides(call)
    if (override !== undefined) return override
    const list = /^\/conversations\?campaign_id=(cmp_\w+)$/.exec(call.url)
    if (list !== null) {
      return { status: 200, body: threadPage([1, 2].map((n) => threadBody(`${list[1]}-${n}`, { campaign_id: list[1] }))) }
    }
    return defaultRoute(call)
  }
}

/** Every web-storage key and value, scanned for `text`. */
function expectNotStored(text: string): void {
  for (const area of [localStorage, sessionStorage]) {
    for (let i = 0; i < area.length; i += 1) {
      const key = area.key(i) ?? ''
      expect(`${key}=${area.getItem(key) ?? ''}`).not.toContain(text)
    }
  }
}

describe('the campaign line (section 7.7, critic 5)', () => {
  it('404, 403 and an archived campaign read alike: one generic line, no thread list (T1-6, M1-6)', async () => {
    const trees: string[] = []
    const answers: Reply[] = [{ status: 404 }, { status: 403 }, { status: 200, body: campaignBody('cmp_A', { archived_at: '2026-09-17T00:00:00Z' }) }]
    for (const answer of answers) {
      const { server } = await mount({ route: serve(({ url }) => (url === '/campaigns/cmp_A' ? answer : undefined)) })
      expect(await screen.findByText("That campaign isn't available.")).toBeInTheDocument()
      expectNoLiveRegion()
      expect(server.lines().filter((l) => l.includes('/conversations'))).toEqual([])
      trees.push(roleTree())
      cleanup()
    }
    expect(new Set(trees).size).toBe(1)
    expect(trees[0]).toContain('button "Continue without a campaign"')
    expect(trees[0]).not.toMatch(/archiv|Conversations|legacy/i)
  })

  it('reads Loading campaign…, then the failure with a Retry that stays mounted and keeps focus through its request (§11)', async () => {
    const { server } = await mount({ route: serve(({ url }) => (url === '/campaigns/cmp_A' ? 'defer' : undefined)) })
    expect(screen.getByText('Loading campaign…')).toBeInTheDocument()
    expectNoLiveRegion()
    act(() => server.calls[0].reply({ status: 503 }))
    const retry = await screen.findByRole('button', { name: 'Retry' })
    expect(screen.getByText("Couldn't load campaigns")).toBeInTheDocument()
    expectNoLiveRegion()
    await userEvent.click(retry)
    expect(server.lines().filter((l) => l === 'GET /campaigns/cmp_A')).toHaveLength(2)
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).toHaveFocus()
    expectNoLiveRegion()
    await userEvent.click(retry)
    expect(server.lines().filter((l) => l === 'GET /campaigns/cmp_A')).toHaveLength(2)
    act(() => server.calls[1].reply({ status: 200, body: campaignBody('cmp_A') }))
    expect(await screen.findByText('Campaign: Name of cmp_A')).toHaveFocus()
    expectNoLiveRegion()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it.each([['failed', { status: 503 }], ['unavailable', { status: 404 }]] as const)(
    'when %s, Continue without a campaign clears it: the legacy GM list is back and focus is on the GM chip (T2-13)',
    async (_state, answer) => {
      const onNavigate = vi.fn()
      await mount({ onNavigate, route: serve(({ url }) => (url === '/campaigns/cmp_A' ? answer : undefined)) })
      await userEvent.click(await screen.findByRole('button', { name: 'Continue without a campaign' }))
      expect(await screen.findByRole('button', { name: 'A legacy GM question about the heist' })).toBeInTheDocument()
      expect(live.c.selection.kind).toBe('none')
      expect(screen.getByRole('button', { name: /GM$/ })).toHaveFocus()
      expect(onNavigate).not.toHaveBeenCalled()
    },
  )
})

describe('the campaign thread list', () => {
  it('shows the campaign and only its threads; New conversation opens none and makes no local row (M2-1b)', async () => {
    const onNavigate = vi.fn()
    const { server, store } = await mount({ onNavigate, route: serve() })
    expect(await screen.findByText('Campaign: Name of cmp_A')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Thread cmp_A-1' })).toBeInTheDocument()
    expect(server.lines()).toContain('GET /conversations?campaign_id=cmp_A')
    expect(screen.queryByRole('button', { name: 'A legacy GM question about the heist' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Thread cmp_A-2' }))
    expect(live.nav.conversationId).toBe('cmp_A-2')
    expect(screen.getByRole('button', { name: 'Thread cmp_A-2' })).toHaveAttribute('aria-pressed', 'true')
    const before = store.list('gm').length
    await userEvent.click(screen.getByRole('button', { name: 'New conversation' }))
    expect(live.nav.conversationId).toBeNull()
    expect(store.list('gm')).toHaveLength(before)
    expect(onNavigate).toHaveBeenCalledTimes(2)
  })

  it('never draws a row of the previous campaign, on any render (T2-4, LIB-25), with its positive control', async () => {
    const heldA = serve(({ url }) => (url === '/conversations?campaign_id=cmp_A' ? 'defer' : undefined))
    const replyA = (calls: Call[]) => act(() => calls.find((c) => c.url.includes('campaign_id=cmp_A'))
      ?.reply({ status: 200, body: threadPage([threadBody('cmp_A-1')]) }))
    const control = await mount({ route: heldA })
    await waitFor(() => expect(control.server.lines()).toContain('GET /conversations?campaign_id=cmp_A'))
    const controlSeen = watchAdded('Thread cmp_A-')
    replyA(control.server.calls)
    expect(await screen.findByRole('button', { name: 'Thread cmp_A-1' })).toBeInTheDocument()
    expect(controlSeen()).toBeGreaterThan(0)
    cleanup()

    const { server } = await mount({ route: heldA })
    await waitFor(() => expect(server.lines()).toContain('GET /conversations?campaign_id=cmp_A'))
    const seen = watchAdded('Thread cmp_A-')
    await act(async () => { await live.c.selectCampaign(CampaignSchema.parse(campaignBody('cmp_B'))) })
    expect(await screen.findByRole('button', { name: 'Thread cmp_B-1' })).toBeInTheDocument()
    replyA(server.calls)
    await act(async () => {})
    expect(seen()).toBe(0)
    expect(screen.getByRole('button', { name: 'Thread cmp_B-2' })).toBeInTheDocument()
  })

  it.each(['a re-read', 'a pick from the loaded list'] as const)(
    "after A's rows are drawn, a switch to B by %s never commits an A row beside B's line (P11, LIB-25)",
    async (path) => {
      const page = { schema_version: 1, items: [campaignBody('cmp_A'), campaignBody('cmp_B')], next_cursor: null }
      const { server } = await mount({ route: serve(({ url }) => (url === '/campaigns' && path !== 'a re-read' ? { status: 200, body: page } : undefined)) })
      expect(await screen.findByRole('button', { name: 'Thread cmp_A-1' })).toBeInTheDocument()
      if (path !== 'a re-read') {
        act(() => live.c.loadCampaigns())
        await waitFor(() => expect(live.c.list.kind).toBe('ready'))
      }
      const commits: string[] = []
      live.onCommit = () => {
        const text = document.body.textContent ?? ''
        if (text.includes('Campaign: Name of cmp_B')) commits.push(text.includes('Thread cmp_A-') ? 'B line with an A row' : 'B line')
      }
      await act(async () => { await live.c.selectCampaign(CampaignSchema.parse(campaignBody('cmp_B'))) })
      expect(await screen.findByRole('button', { name: 'Thread cmp_B-1' })).toBeInTheDocument()
      expect(server.lines().filter((l) => l === 'GET /campaigns/cmp_B')).toHaveLength(path === 'a re-read' ? 1 : 0)
      expect(commits.length).toBeGreaterThan(0)
      expect(commits.filter((c) => c !== 'B line')).toEqual([])
    },
  )

  it('loading, failure, Retry and Load more: visible text, focus kept, and none of them navigates (T2-11)', async () => {
    const onNavigate = vi.fn()
    let lists = 0
    const { server } = await mount({
      onNavigate,
      route: serve(({ url }) => {
        if (!url.startsWith('/conversations?campaign_id=cmp_A')) return undefined
        lists += 1
        if (lists === 1) return { status: 503 }
        if (lists === 2) return 'defer'
        return { status: 200, body: threadPage([threadBody('cmp_A-3')]) }
      }),
    })
    const retry = await screen.findByRole('button', { name: 'Retry' })
    expect(screen.getByText("Couldn't load conversations")).toBeInTheDocument()
    expectNoLiveRegion()
    await userEvent.click(retry)
    expect(screen.getByText('Loading conversations…')).toBeInTheDocument()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).toHaveFocus()
    expectNoLiveRegion()
    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: threadPage([threadBody('cmp_A-1')], 'more_1') }))
    expect(await screen.findByRole('button', { name: 'Thread cmp_A-1' })).toBeInTheDocument()
    expect(screen.getByText('Conversations')).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument()
    expectNoLiveRegion()
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    expect(await screen.findByRole('button', { name: 'Thread cmp_A-3' })).toBeInTheDocument()
    expect(server.lines().at(-1)).toBe('GET /conversations?campaign_id=cmp_A&cursor=more_1')
    expect(screen.getAllByRole('button', { name: /^Thread / }).map((b) => b.textContent)).toEqual(['Thread cmp_A-1', 'Thread cmp_A-3'])
    expect(onNavigate).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Thread cmp_A-1' }))
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })

  it('renames through the server: its title, an inline 422, a 503 that keeps the text with Retry, a 404 that removes the row (T2-7)', async () => {
    const onNavigate = vi.fn()
    const answers: Reply[] = [{ status: 422 }, { status: 503 }, { status: 200, body: threadBody('cmp_A-1', { title: 'Server title' }) }, { status: 404 }]
    const { server } = await mount({ onNavigate, route: serve(({ method }) => (method === 'PATCH' ? answers.shift() : undefined)) })
    await userEvent.click(await screen.findByRole('button', { name: 'Rename Thread cmp_A-1' }))
    const input = screen.getByRole('textbox', { name: 'Conversation title for Thread cmp_A-1' })
    await userEvent.clear(input)
    await userEvent.type(input, 'The Smuggler Queen{Enter}')
    await waitFor(() => expect(input).toHaveAttribute('aria-invalid', 'true'))
    expect(input).toHaveAccessibleDescription('A title is 1 to 200 characters on one line.')
    expectNoLiveRegion()
    await userEvent.type(input, 's{Enter}')
    expect(await screen.findByText("Couldn't rename the conversation.")).toBeInTheDocument()
    expect(input).toHaveValue('The Smuggler Queens')
    expectNoLiveRegion()
    expect(input).not.toHaveAttribute('aria-invalid')
    expectNotStored('Smuggler Queen')
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: 'Server title' })).toBeInTheDocument()
    expect(server.calls.filter((c) => c.method === 'PATCH').map((c) => JSON.parse(c.body ?? '{}'))).toEqual([
      { schema_version: 1, title: 'The Smuggler Queen' },
      { schema_version: 1, title: 'The Smuggler Queens' },
      { schema_version: 1, title: 'The Smuggler Queens' },
    ])
    await userEvent.click(screen.getByRole('button', { name: 'Rename Thread cmp_A-2' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Conversation title for Thread cmp_A-2' }), '!{Enter}')
    await waitFor(() => expect(screen.queryByRole('button', { name: /cmp_A-2/ })).toBeNull())
    expectNotStored('Smuggler Queen')
    expect(onNavigate).not.toHaveBeenCalled()
  })
})

describe('an identity change during a thread read (section 7.5, pr178-mpost N-2)', () => {
  it("ada's held list is aborted as a browser aborts it; bob never sees her rows or a failure, and his own read renders", async () => {
    let lists = 0
    const { server, switchTo } = await mount({
      route: serve(({ url }) => {
        if (url !== '/conversations?campaign_id=cmp_A') return undefined
        lists += 1
        return lists === 1 ? 'defer' : { status: 200, body: threadPage([threadBody('cmp_A-9', { title: "Bob's thread" })]) }
      }),
    })
    expect(await screen.findByText('Loading conversations…')).toBeInTheDocument()
    const held = server.calls.find((c) => c.url === '/conversations?campaign_id=cmp_A')
    expect(live.lists()).toBe(1)
    const seen: string[] = []
    const observer = new MutationObserver(() => {
      const text = document.body.textContent ?? ''
      if (text.includes('Thread cmp_A-')) seen.push('an ada row')
      if (text.includes("Couldn't load conversations")) seen.push('a failure')
    })
    observer.observe(document.body, { subtree: true, childList: true, characterData: true })
    await switchTo('bob@example.com')
    expect(held?.signal?.aborted).toBe(true)
    expect(live.lists()).toBe(0) // nothing of ada's is kept in memory either
    held?.reply({ status: 200, body: threadPage([threadBody('cmp_A-1')]) })
    expect(live.c.selection.kind).toBe('none')
    await act(async () => { await live.c.selectCampaign(CampaignSchema.parse(campaignBody('cmp_A'))) })
    expect(await screen.findByRole('button', { name: "Bob's thread" })).toBeInTheDocument()
    observer.disconnect()
    expect(seen).toEqual([])
  })
})
