/**
 * TavernScreen.test.tsx -- the campaign screen at /tavern, mounted alone
 * (agent-forge-harness-74j, brief section 7.2, T-16 to T-18).
 *
 * The harness copies `LeftNavCampaign.test.tsx:29-117`: real AppNavProvider,
 * CurrentUserProvider (identityChannelFactory stubbed), ConversationStoreProvider
 * with a MemoryConversationStore, and CampaignProvider with `fetchImpl`. A
 * `Host` renders `<TavernScreen />` only while `useAppNav().screen ===
 * 'tavern'`, so an in-app arrival (`openTavern`) and a cold arrival
 * (`initialScreen="tavern"`, `navIntent` `'replace'`) are both exercised.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { AppNavContext, AppNavProvider, useAppNav, type AppNavState } from './AppNav'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
import type { IdentityChannelLike } from './identityBroadcast'
import { TavernScreen } from './TavernScreen'

// ── Harness (copied from LeftNavCampaign.test.tsx:29-117) ──────────────────

function campaignBody(id: string, over: Record<string, unknown> = {}) {
  return {
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false, ...over,
  }
}

type Reply = { status: number; body?: unknown } | 'network' | 'defer'
interface Call { url: string; method: string; body: string | null; reply: (r: Reply) => void }
type Route = (call: Call) => Reply

const defaultRoute: Route = ({ url }) => {
  if (url === '/campaigns') return { status: 200, body: { schema_version: 1, items: [], next_cursor: null } }
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (one !== null) return { status: 200, body: campaignBody(one[1]) }
  return { status: 404, body: {} }
}

function stubServer(route: Route) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null,
      reply: (r) => (r === 'network' ? reject(new TypeError('Failed to fetch'))
        : resolve(new Response(JSON.stringify(r === 'defer' ? {} : (r.body ?? {})), { status: r === 'defer' ? 200 : r.status }))),
    }
    calls.push(call)
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  return { fetchImpl, calls, lines: () => calls.map((c) => `${c.method} ${c.url}`) }
}

const live = {} as { c: CampaignContextValue; nav: AppNavState; raw: AppNavState; user: CurrentUserContextValue }
function Probe(): null {
  const c = useCampaign()
  const nav = useAppNav()
  const user = useCurrentUser()
  React.useLayoutEffect(() => {
    live.c = c
    live.nav = nav
    live.user = user
  })
  return null
}

/** The RAW AppNavContext, read from OUTSIDE CampaignProvider's re-provision
 * (R-5): below the provider, `conversationId` is the *visible* id (A-12), so
 * a mutation that drops TavernScreen's own null can hide behind it. Mirrors
 * AppRoot's tower: placed between ConversationStoreProvider and
 * CampaignProvider. */
function RawProbe(): null {
  const raw = React.useContext(AppNavContext)
  React.useLayoutEffect(() => {
    live.raw = raw
  })
  return null
}

/** Renders TavernScreen only while the screen is 'tavern' and the account is
 * settled, as App.tsx's auth gates do (it never renders a screen while
 * `authStatus === 'checking'`, so `enabled` is never read stale-false). */
function Host(): React.JSX.Element | null {
  const { screen } = useAppNav()
  const { authStatus } = useCurrentUser()
  if (authStatus === 'checking') return null
  return screen === 'tavern' ? <TavernScreen /> : null
}

interface MountOptions { route?: Route; initialScreen?: 'tavern' | 'workspace'; campaign?: string }
async function mount({ route = defaultRoute, initialScreen = 'workspace', campaign }: MountOptions = {}) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  const server = stubServer(route)
  const store = new MemoryConversationStore()
  const view = render(
    <AppNavProvider initialScreen={initialScreen} initialMode="gm">
      <CurrentUserProvider identityChannelFactory={() => {
        const channel: IdentityChannelLike = { postMessage: () => {}, close: () => {}, onmessage: null }
        return channel
      }}>
        <ConversationStoreProvider store={store}>
          <RawProbe />
          <CampaignProvider
            fetchImpl={server.fetchImpl}
            restore={campaign === undefined ? null : { campaignId: campaign, conversationId: null }}
          >
            <Host />
            <Probe />
          </CampaignProvider>
        </ConversationStoreProvider>
      </CurrentUserProvider>
    </AppNavProvider>,
  )
  await waitFor(() => expect(live.c.enabled).toBe(true))
  return { server, view }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('TavernScreen mounted alone (74j, T-16 to T-18)', () => {
  it('one main and one h1 named Your Campaigns in the first commit, in every list state; focus only on an in-app arrival', async () => {
    const { server } = await mount({ route: () => 'defer' })
    act(() => live.nav.openTavern?.())
    expect(screen.getAllByRole('main')).toHaveLength(1)
    const heading = await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(screen.getAllByRole('heading')).toHaveLength(1)
    expect(heading).toHaveFocus()

    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: { schema_version: 1, items: [], next_cursor: null } }))
    expect(await screen.findByText('Create your first campaign', { exact: false })).toBeInTheDocument()
    expect(screen.getAllByRole('heading')).toHaveLength(1)
    expect(screen.getAllByRole('main')).toHaveLength(1)
  })

  it('a cold arrival (navIntent replace) moves no focus', async () => {
    await mount({ initialScreen: 'tavern' })
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(document.activeElement).toBe(document.body)
  })

  it('after a failed read, Retry lands focus on the h1 when the list answers', async () => {
    let n = 0
    const { server } = await mount({
      route: (call) => (call.url === '/campaigns' ? (n++ === 0 ? { status: 503 } : 'defer') : defaultRoute(call)),
    })
    act(() => live.nav.openTavern?.())
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: { schema_version: 1, items: [], next_cursor: null } }))
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveFocus())
    expect(screen.getAllByRole('heading')).toHaveLength(1)
  })

  it('CampaignPicker without pageHeading still renders its own h2 and no h1 (control)', async () => {
    const { CampaignPicker } = await import('./CampaignPicker')
    vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
    const server = stubServer(defaultRoute)
    render(
      <AppNavProvider>
        <CurrentUserProvider>
          <ConversationStoreProvider store={new MemoryConversationStore()}>
            <CampaignProvider fetchImpl={server.fetchImpl}>
              <CampaignPicker />
              <Probe />
            </CampaignProvider>
          </ConversationStoreProvider>
        </CurrentUserProvider>
      </AppNavProvider>,
    )
    // Settle the enabled/userId remount (the picker is keyed on both) before
    // querying: querying mid-remount can hand back a node React then removes.
    await waitFor(() => expect(live.c.enabled).toBe(true))
    expect(screen.getByRole('heading', { name: 'Your campaigns', level: 2 })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull()
  })

  it('adds no live region of its own: the picker status node is the only one', async () => {
    await mount()
    act(() => live.nav.openTavern?.())
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    const nodes = document.querySelectorAll('[role="status"], [role="alert"], [aria-live]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0]?.className).toContain('campaign-picker__status')
  })

  it('Tab order is the picker, then Continue without a campaign (when offered), then Back to chat', async () => {
    await mount({ campaign: 'cmp_A' })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => live.nav.openTavern?.())
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    // textContent (unlike the accessible name) also picks up the Back
    // button's hidden icon ligature, so match by substring rather than
    // equality.
    const buttons = screen.getAllByRole('button').map((b) => b.textContent ?? '')
    const continueIndex = buttons.findIndex((t) => t.includes('Continue without a campaign'))
    const backIndex = buttons.findIndex((t) => t.includes('Back to chat'))
    expect(continueIndex).toBeGreaterThan(-1)
    expect(backIndex).toBe(buttons.length - 1)
    expect(continueIndex).toBeLessThan(backIndex)
  })
})

describe('navigation on selection change (74j, T-9, T-10, T-12a mutation coverage)', () => {
  it('a pick nulls the open conversation and enters the GM workspace (R-1)', async () => {
    await mount({
      route: (call) => (call.url === '/campaigns'
        ? { status: 200, body: { schema_version: 1, items: [campaignBody('cmp_A')], next_cursor: null } }
        : defaultRoute(call)),
    })
    act(() => { live.raw.setConversationId('cnv_legacy') })
    act(() => live.nav.openTavern?.())
    await userEvent.click(await screen.findByRole('button', { name: 'Name of cmp_A' }))
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    expect(live.raw.conversationId).toBeNull()
    expect(live.nav.screen).toBe('workspace')
    expect(live.nav.mode).toBe('gm')
  })

  it('Continue without a campaign nulls the conversation and enters uncampaigned GM', async () => {
    await mount({ campaign: 'cmp_A' })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => { live.raw.setConversationId('cnv_legacy') })
    act(() => live.nav.openTavern?.())
    await userEvent.click(await screen.findByRole('button', { name: 'Continue without a campaign' }))
    await waitFor(() => expect(live.c.selection.kind).toBe('none'))
    expect(live.raw.conversationId).toBeNull()
    expect(live.nav.screen).toBe('workspace')
  })
})
