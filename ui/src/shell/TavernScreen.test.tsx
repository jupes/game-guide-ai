/**
 * TavernScreen.test.tsx -- the campaign screen at /tavern, mounted alone
 * (agent-forge-harness-74j, brief section 7.2, T-16 to T-18; 30c PR-1, S-1 to
 * S-18).
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
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { AppNavContext, AppNavProvider, useAppNav, type AppNavState } from './AppNav'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
import type { IdentityChannelLike } from './identityBroadcast'
import { TAVERN_MAX_PAGES } from './tavernOrder'
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
/** Activity on day `n` of September 2026, so a higher `n` is more recent. */
const day = (n: number): string => `2026-09-${String(n).padStart(2, '0')}T12:00:00Z`
const page = (items: unknown[], next: string | null = null) => ({ schema_version: 1, items, next_cursor: next })
const EMPTY_PAGE = page([])

type Reply = { status: number; body?: unknown } | 'network' | 'defer'
interface Call { url: string; method: string; body: string | null; reply: (r: Reply) => void }
type Route = (call: Call) => Reply

/** One of the caller's seats (PlayerSeat, 30c PR-2). */
function seatBody(id: string, over: Record<string, unknown> = {}) {
  return {
    schema_version: 1, campaign_id: id, campaign_name: `Table of ${id}`, alias: 'Brannoc',
    accepted_at: '2026-09-10T12:00:00Z', confirmed: true, tone: null, game_system: 'dnd5e',
    avatar_icon: 'castle', avatar_tone: 'gold', concluded: false, last_played_at: null, live: false, ...over,
  }
}

const defaultRoute: Route = ({ url }) => {
  if (url === '/campaigns') return { status: 200, body: EMPTY_PAGE }
  if (url.startsWith('/seats')) return { status: 200, body: EMPTY_PAGE }
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (one !== null) return { status: 200, body: campaignBody(one[1]) }
  return { status: 404, body: {} }
}
/** A list read answers `items`; everything else is the default. */
const listOf = (items: unknown[], next: string | null = null): Route => (call) => (
  call.method === 'GET' && call.url === '/campaigns' ? { status: 200, body: page(items, next) } : defaultRoute(call)
)
/** Every `GET /seats` answers `items` (one page); everything else is `base`. */
const seatedWith = (items: unknown[], base: Route = defaultRoute): Route => (call) => (
  call.method === 'GET' && call.url.startsWith('/seats') ? { status: 200, body: page(items) } : base(call)
)
/** The campaigns read never answers; seats (and the rest) are the default. */
const deferCampaigns: Route = (call) => (call.url === '/campaigns' ? 'defer' : defaultRoute(call))

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
  return {
    fetchImpl,
    calls,
    lines: () => calls.map((c) => `${c.method} ${c.url}`),
    /** The campaign family only: the seat read (30c PR-2) is asserted on its own. */
    campaignLines: () => calls.filter((c) => !c.url.startsWith('/seats')).map((c) => `${c.method} ${c.url}`),
    posts: () => calls.filter((c) => c.method === 'POST'),
  }
}

const live = {} as {
  c: CampaignContextValue; nav: AppNavState; raw: AppNavState; user: CurrentUserContextValue
  /** What the status node held in the first commit that had one. */
  statusAtMount: string | undefined
}
/** Rendered after the screen: its layout effect sees the first commit's status node. */
function StatusAtMount(): null {
  React.useLayoutEffect(() => {
    const node = document.querySelector('.tavern-screen__status')
    if (live.statusAtMount === undefined && node !== null) live.statusAtMount = node.textContent ?? ''
  })
  return null
}
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
  return screen === 'tavern' ? <><TavernScreen /><StatusAtMount /></> : null
}

interface MountOptions {
  route?: Route
  initialScreen?: 'tavern' | 'workspace'
  campaign?: string
  seed?: (store: MemoryConversationStore) => void
  role?: 'dm' | 'player'
}
async function mount({ route = defaultRoute, initialScreen = 'workspace', campaign, seed, role = 'dm' }: MountOptions = {}) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role } })
  live.statusAtMount = undefined
  const server = stubServer(route)
  const store = new MemoryConversationStore()
  seed?.(store)
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
  await waitFor(() => expect(live.c.enabled).toBe(role === 'dm'))
  return { server, view, store }
}

const openTavern = (): void => act(() => live.nav.openTavern?.())
const prepButton = (id: string): HTMLElement => screen.getByRole('button', { name: `Prep Name of ${id}` })
const findPrep = (id: string): Promise<HTMLElement> => screen.findByRole('button', { name: `Prep Name of ${id}` })
const statusNode = (): HTMLElement => document.querySelector('.tavern-screen__status') as HTMLElement

/** Every distinct non-empty text the one status node held, in order. */
function watchStatus(): { texts: string[]; stop: () => void } {
  const texts: string[] = []
  const note = (): void => {
    const text = document.querySelector('.tavern-screen__status')?.textContent ?? ''
    if (text !== '' && text !== texts.at(-1)) texts.push(text)
  }
  const observer = new MutationObserver(note)
  observer.observe(document.body, { subtree: true, childList: true, characterData: true })
  return { texts, stop: () => observer.disconnect() }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('TavernScreen mounted alone (74j, T-16 to T-18)', () => {
  it('one main and one h1 named Your Campaigns in the first commit, in every list state; focus only on an in-app arrival', async () => {
    const { server } = await mount({ route: deferCampaigns })
    act(() => live.nav.openTavern?.())
    expect(screen.getAllByRole('main')).toHaveLength(1)
    const heading = await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(heading).toHaveFocus()

    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: EMPTY_PAGE }))
    expect(await screen.findByText('Create your first campaign', { exact: false })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
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
    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: EMPTY_PAGE }))
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveFocus())
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
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

  it('Tab order is the header (New Campaign, Continue when offered, Back to chat), then the cards', async () => {
    await mount({ campaign: 'cmp_A', route: listOf([campaignBody('cmp_A')]) })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => live.nav.openTavern?.())
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    await findPrep('cmp_A')
    // textContent (unlike the accessible name) also picks up the buttons'
    // hidden icon ligatures, so match by substring rather than equality.
    const buttons = screen.getAllByRole('button').map((b) => b.textContent ?? '')
    const at = (text: string): number => buttons.findIndex((t) => t.includes(text))
    expect(at('New Campaign')).toBe(0)
    expect(at('Continue without a campaign')).toBe(1)
    expect(at('Back to chat')).toBe(2)
    expect(at('Prep')).toBeGreaterThan(2)
    expect(at('Create campaign')).toBeGreaterThan(at('Prep'))
  })
})

describe('the status node and the headings (S-1)', () => {
  it('S-1 exactly one h1 and one live region in every state, and the node is empty at mount', async () => {
    const check = (): void => {
      expect(screen.getAllByRole('heading', { level: 1, name: 'Your Campaigns' })).toHaveLength(1)
      expect(document.querySelectorAll('[role="status"], [role="alert"], [aria-live]')).toHaveLength(1)
    }
    // loading
    const loading = await mount({ route: deferCampaigns })
    openTavern()
    await screen.findByRole('heading', { level: 1 })
    expect(live.statusAtMount).toBe('')
    expect(statusNode()).toHaveTextContent('Loading campaigns…')
    check()
    loading.view.unmount()
    // loaded with cards (active, dormant and concluded)
    const cards = await mount({
      route: listOf([
        campaignBody('cmp_A', { last_activity_at: day(5) }),
        campaignBody('cmp_D', { dormant: true, last_activity_at: day(1) }),
        campaignBody('cmp_C', { concluded_at: day(6) }),
      ]),
    })
    openTavern()
    await findPrep('cmp_A')
    check()
    cards.view.unmount()
    // empty
    const empty = await mount()
    openTavern()
    await screen.findByText('Create your first campaign', { exact: false })
    check()
    empty.view.unmount()
    // error
    await mount({ route: (call) => (call.url === '/campaigns' ? { status: 503 } : defaultRoute(call)) })
    openTavern()
    await screen.findByRole('button', { name: 'Retry' })
    check()
  })

  it('S-1 says "Loading campaigns…" once at the start and "Campaigns loaded" once at the end, however many pages it read', async () => {
    const route: Route = (call) => {
      if (call.method === 'GET' && call.url === '/campaigns') return { status: 200, body: page([campaignBody('cmp_A')], 'c2') }
      if (call.method === 'GET' && call.url === '/campaigns?cursor=c2') return { status: 200, body: page([campaignBody('cmp_B')]) }
      return defaultRoute(call)
    }
    await mount({ route })
    const watch = watchStatus()
    openTavern()
    await findPrep('cmp_B')
    await waitFor(() => expect(statusNode()).toHaveTextContent('Campaigns loaded'))
    watch.stop()
    expect(watch.texts).toEqual(['Loading campaigns…', 'Campaigns loaded'])
  })

  it('S-1 a revisit announces the new read too, with the node empty at mount', async () => {
    await mount({ route: listOf([campaignBody('cmp_A')]) })
    openTavern()
    await findPrep('cmp_A')
    await waitFor(() => expect(statusNode()).toHaveTextContent('Campaigns loaded'))
    await userEvent.click(screen.getByRole('button', { name: 'Back to chat' }))
    live.statusAtMount = undefined
    const watch = watchStatus()
    openTavern()
    await waitFor(() => expect(statusNode()).toHaveTextContent('Campaigns loaded'))
    watch.stop()
    expect(live.statusAtMount).toBe('')
    expect(watch.texts).toEqual(['Loading campaigns…', 'Campaigns loaded'])
  })
})

describe('focus (S-2 to S-4, S-16)', () => {
  const two = listOf([
    campaignBody('cmp_old', { last_activity_at: day(2) }),
    campaignBody('cmp_new', { last_activity_at: day(9) }),
  ])

  it('S-2 an in-app arrival focuses the h1, then the top card Prep once the list settles; an empty list leaves it on the h1', async () => {
    const { server } = await mount({ route: (call) => (call.url === '/campaigns' ? 'defer' : defaultRoute(call)) })
    openTavern()
    const heading = await screen.findByRole('heading', { level: 1 })
    expect(heading).toHaveFocus()
    act(() => server.calls[server.calls.length - 1].reply({
      status: 200, body: page([campaignBody('cmp_old', { last_activity_at: day(2) }), campaignBody('cmp_new', { last_activity_at: day(9) })]),
    }))
    await waitFor(() => expect(prepButton('cmp_new')).toHaveFocus())
  })

  it('S-2 with an empty list, focus stays on the h1', async () => {
    await mount()
    openTavern()
    await screen.findByText('Create your first campaign', { exact: false })
    expect(screen.getByRole('heading', { level: 1 })).toHaveFocus()
  })

  it('S-3 a cold arrival moves focus nowhere, before or after the list settles', async () => {
    const { server } = await mount({ initialScreen: 'tavern', route: (call) => (call.url === '/campaigns' ? 'defer' : defaultRoute(call)) })
    await screen.findByRole('heading', { level: 1 })
    expect(document.activeElement).toBe(document.body)
    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: page([campaignBody('cmp_A')]) }))
    await findPrep('cmp_A')
    await act(async () => {})
    expect(document.activeElement).toBe(document.body)
  })

  it('S-4 focus the user moved before the list settled is not stolen', async () => {
    const { server } = await mount({ route: (call) => (call.url === '/campaigns' ? 'defer' : defaultRoute(call)) })
    openTavern()
    await screen.findByRole('heading', { level: 1 })
    await userEvent.tab()
    const newCampaign = screen.getByRole('button', { name: 'New Campaign' })
    expect(newCampaign).toHaveFocus()
    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: page([campaignBody('cmp_A')]) }))
    await findPrep('cmp_A')
    await act(async () => {})
    expect(newCampaign).toHaveFocus()
  })

  it('S-4 and once per visit: a later change in the list does not pull focus back to a card', async () => {
    await mount({ route: two })
    openTavern()
    await waitFor(() => expect(prepButton('cmp_new')).toHaveFocus())
    // Focus is back on the heading (where the automatic move would act again).
    act(() => screen.getByRole('heading', { level: 1 }).focus())
    act(() => live.c.loadCampaigns())
    await waitFor(() => expect(live.c.list.kind).toBe('ready'))
    await act(async () => {})
    expect(screen.getByRole('heading', { level: 1 })).toHaveFocus()
  })

  it('S-16 New Campaign focuses the Campaign name field', async () => {
    await mount({ route: two })
    openTavern()
    await findPrep('cmp_new')
    await userEvent.click(screen.getByRole('button', { name: 'New Campaign' }))
    expect(screen.getByRole('textbox', { name: 'Campaign name' })).toHaveFocus()
  })
})

describe('reading the whole list (S-5)', () => {
  it('S-5 follows the cursor on its own: a two-page list is two reads and both pages show', async () => {
    const route: Route = (call) => {
      if (call.method === 'GET' && call.url === '/campaigns') return { status: 200, body: page([campaignBody('cmp_A', { last_activity_at: day(3) })], 'c2') }
      if (call.method === 'GET' && call.url === '/campaigns?cursor=c2') return { status: 200, body: page([campaignBody('cmp_B', { last_activity_at: day(4) })]) }
      return defaultRoute(call)
    }
    const { server } = await mount({ route })
    openTavern()
    await findPrep('cmp_A')
    await findPrep('cmp_B')
    await act(async () => {})
    expect(server.campaignLines()).toEqual(['GET /campaigns', 'GET /campaigns?cursor=c2'])
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('S-5 a server that always returns a cursor stops at exactly TAVERN_MAX_PAGES reads and offers Load more, one page a press', async () => {
    const route: Route = (call) => {
      const read = /^\/campaigns(?:\?cursor=c(\d+))?$/.exec(call.url)
      if (call.method === 'GET' && read !== null) {
        const n = read[1] === undefined ? 0 : Number(read[1])
        return { status: 200, body: page([campaignBody(`cmp_p${n}`, { last_activity_at: day(20 - n) })], `c${n + 1}`) }
      }
      return defaultRoute(call)
    }
    const { server } = await mount({ route })
    openTavern()
    const loadMore = await screen.findByRole('button', { name: 'Load more' })
    await act(async () => {})
    expect(server.campaignLines()).toHaveLength(TAVERN_MAX_PAGES)
    // The read is complete at the ceiling: announced, and the top card takes focus.
    expect(statusNode()).toHaveTextContent('Campaigns loaded')
    await waitFor(() => expect(prepButton('cmp_p0')).toHaveFocus())
    await userEvent.click(loadMore)
    await waitFor(() => expect(server.campaignLines()).toHaveLength(TAVERN_MAX_PAGES + 1))
    await act(async () => {})
    expect(server.campaignLines()).toHaveLength(TAVERN_MAX_PAGES + 1)
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument()
  })

  it('S-5 a Load more press is announced as a read in flight, keeps the button mounted and disabled, and ends with Campaigns loaded', async () => {
    let held: Call | null = null
    const route: Route = (call) => {
      const read = /^\/campaigns(?:\?cursor=c(\d+))?$/.exec(call.url)
      if (call.method === 'GET' && read !== null) {
        const n = read[1] === undefined ? 0 : Number(read[1])
        if (n >= TAVERN_MAX_PAGES) {
          held = call
          return 'defer'
        }
        return { status: 200, body: page([campaignBody(`cmp_p${n}`, { last_activity_at: day(20 - n) })], `c${n + 1}`) }
      }
      return defaultRoute(call)
    }
    await mount({ route })
    openTavern()
    const loadMore = await screen.findByRole('button', { name: 'Load more' })
    await userEvent.click(loadMore)
    expect(held).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Load more' })).toHaveAttribute('aria-disabled', 'true')
    expect(statusNode()).toHaveTextContent('Loading campaigns…')
    act(() => (held as Call | null)?.reply({ status: 200, body: page([campaignBody('cmp_p4', { last_activity_at: day(10) })]) }))
    await findPrep('cmp_p4')
    await waitFor(() => expect(statusNode()).toHaveTextContent('Campaigns loaded'))
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('S-5 a failed follow-up page keeps the cards, says so and stops; Retry reads that page again', async () => {
    let failing = true
    const route: Route = (call) => {
      if (call.method === 'GET' && call.url === '/campaigns') return { status: 200, body: page([campaignBody('cmp_A')], 'c2') }
      if (call.method === 'GET' && call.url === '/campaigns?cursor=c2') return failing ? { status: 503 } : { status: 200, body: page([campaignBody('cmp_B')]) }
      return defaultRoute(call)
    }
    const { server } = await mount({ route })
    openTavern()
    await screen.findByRole('button', { name: 'Retry' })
    await act(async () => {})
    expect(server.campaignLines()).toEqual(['GET /campaigns', 'GET /campaigns?cursor=c2'])
    expect(prepButton('cmp_A')).toBeInTheDocument()
    expect(screen.getByText("Couldn't load campaigns", { selector: 'p:not([role])' })).toBeInTheDocument()
    failing = false
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await findPrep('cmp_B')
    expect(server.campaignLines()).toEqual(['GET /campaigns', 'GET /campaigns?cursor=c2', 'GET /campaigns?cursor=c2'])
  })
})

describe('the active list (S-6, S-7)', () => {
  const many = (n: number) => Array.from({ length: n }, (_, i) => (
    campaignBody(`cmp_${String(i + 1).padStart(2, '0')}`, { last_activity_at: day(28 - i) })
  ))

  it('S-6 13 campaigns show 12; Show more campaigns shows the 13th with no request and focuses its Prep', async () => {
    const { server } = await mount({ route: listOf(many(13)) })
    openTavern()
    await findPrep('cmp_12')
    expect(screen.queryByRole('button', { name: 'Prep Name of cmp_13' })).toBeNull()
    expect(screen.getAllByRole('button', { name: /^Prep / })).toHaveLength(12)
    const before = server.calls.length
    await userEvent.click(screen.getByRole('button', { name: 'Show more campaigns' }))
    expect(await findPrep('cmp_13')).toHaveFocus()
    expect(screen.getAllByRole('button', { name: /^Prep / })).toHaveLength(13)
    expect(screen.queryByRole('button', { name: 'Show more campaigns' })).toBeNull()
    expect(server.calls).toHaveLength(before)
  })

  it('S-6 exactly 12 campaigns show no Show more button', async () => {
    await mount({ route: listOf(many(12)) })
    openTavern()
    await findPrep('cmp_12')
    expect(screen.queryByRole('button', { name: 'Show more campaigns' })).toBeNull()
  })

  it('S-7 the order follows last activity, not the order the server sent', async () => {
    const items = [
      campaignBody('cmp_a', { last_activity_at: day(1) }),
      campaignBody('cmp_b', { last_activity_at: day(3) }),
      campaignBody('cmp_c', { last_activity_at: day(2) }),
    ]
    await mount({ route: listOf(items) })
    openTavern()
    await findPrep('cmp_a')
    const list = screen.getByRole('list', { name: 'Your campaigns' })
    const names = within(list).getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(names).toEqual(['Name of cmp_b', 'Name of cmp_c', 'Name of cmp_a'])
    expect(live.c.list.kind === 'ready' && live.c.list.items.map((c) => c.campaign_id)).toEqual(['cmp_a', 'cmp_b', 'cmp_c'])
  })

  it('a dormant campaign keeps its recency position and reads as dormant (E-4); an active one has no Mark concluded', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-01T09:00:00Z'))
    const items = [
      campaignBody('cmp_live', { last_activity_at: '2026-09-25T12:00:00Z' }),
      campaignBody('cmp_dormant', { dormant: true, last_activity_at: '2026-08-04T12:00:00Z' }),
      campaignBody('cmp_mid', { last_activity_at: '2026-09-01T12:00:00Z' }),
    ]
    await mount({ route: listOf(items) })
    openTavern()
    await findPrep('cmp_live')
    const list = screen.getByRole('list', { name: 'Your campaigns' })
    expect(within(list).getAllByRole('heading', { level: 2 }).map((h) => h.textContent))
      .toEqual(['Name of cmp_live', 'Name of cmp_mid', 'Name of cmp_dormant'])
    expect(screen.getByText('Dormant · no activity since 4 August')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Mark concluded / })).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Mark concluded Name of cmp_dormant' })).toBeInTheDocument()
  })
})

describe('Concluded (S-8, S-9, S-10, S-11)', () => {
  const withConcluded = (extra: unknown[] = []) => listOf([
    campaignBody('cmp_A', { last_activity_at: day(8) }),
    campaignBody('cmp_D', { dormant: true, last_activity_at: day(1) }),
    campaignBody('cmp_C1', { concluded_at: day(10), last_activity_at: day(7) }),
    campaignBody('cmp_C2', { concluded_at: day(11), last_activity_at: day(6) }),
    ...extra,
  ])
  const concludedRoute = (over: Route = () => 'defer'): Route => (call) => {
    if (call.method === 'POST' && call.url === '/campaigns/cmp_D/conclude') {
      return over(call) === 'defer'
        ? { status: 200, body: campaignBody('cmp_D', { dormant: true, last_activity_at: day(1), concluded_at: day(12) }) }
        : over(call)
    }
    if (call.method === 'POST' && call.url === '/campaigns/cmp_C1/reopen') {
      return { status: 200, body: campaignBody('cmp_C1', { last_activity_at: day(7), concluded_at: null }) }
    }
    return withConcluded()(call)
  }

  it('S-8 the toggle shows the count only with concluded campaigns; aria-expanded toggles and the region is hidden while collapsed', async () => {
    await mount({ route: withConcluded() })
    openTavern()
    await findPrep('cmp_A')
    const toggle = await screen.findByRole('button', { name: 'Concluded (2)' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const region = document.getElementById(toggle.getAttribute('aria-controls') as string) as HTMLElement
    expect(region).toHaveAttribute('aria-label', 'Concluded campaigns')
    expect(region).not.toBeVisible()
    expect(screen.queryByRole('button', { name: 'Prep Name of cmp_C1' })).toBeNull()
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(region).toBeVisible()
    expect(screen.getByRole('button', { name: 'Prep Name of cmp_C1' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Reopen Name of cmp_C1' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^Start Session Name of cmp_C1/ })).toBeNull()
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(region).not.toBeVisible()
  })

  it('S-8 with none concluded there is no toggle and no region (control: the list is there)', async () => {
    await mount({ route: listOf([campaignBody('cmp_A')]) })
    openTavern()
    await findPrep('cmp_A')
    expect(screen.queryByRole('button', { name: /^Concluded/ })).toBeNull()
    expect(document.querySelector('[aria-label="Concluded campaigns"]')).toBeNull()
  })

  it('S-8 the toggle works from the keyboard (Enter and Space)', async () => {
    await mount({ route: withConcluded() })
    openTavern()
    const toggle = await screen.findByRole('button', { name: 'Concluded (2)' })
    toggle.focus()
    await userEvent.keyboard('{Enter}')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await userEvent.keyboard(' ')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('S-8 concluded campaigns are paged by 12 inside the region', async () => {
    const concluded = Array.from({ length: 13 }, (_, i) => campaignBody(`cmp_x${String(i + 1).padStart(2, '0')}`, {
      concluded_at: day(10), last_activity_at: day(28 - i),
    }))
    await mount({ route: listOf(concluded) })
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Concluded (13)' }))
    expect(screen.getAllByRole('button', { name: /^Reopen / })).toHaveLength(12)
    await userEvent.click(screen.getByRole('button', { name: 'Show more concluded campaigns' }))
    expect(screen.getAllByRole('button', { name: /^Reopen / })).toHaveLength(13)
  })

  it('S-9 Mark concluded sends one POST; the card moves to Concluded (3), focus goes to the toggle, and the status reads Marking concluded… then Marked concluded', async () => {
    const { server } = await mount({ route: concludedRoute() })
    openTavern()
    await screen.findByRole('button', { name: 'Concluded (2)' })
    const watch = watchStatus()
    await userEvent.click(await screen.findByRole('button', { name: 'Mark concluded Name of cmp_D' }))
    expect(await screen.findByRole('button', { name: 'Concluded (3)' })).toHaveFocus()
    await waitFor(() => expect(statusNode()).toHaveTextContent('Marked concluded'))
    watch.stop()
    expect(server.posts().map((c) => `${c.method} ${c.url}`)).toEqual(['POST /campaigns/cmp_D/conclude'])
    expect(server.posts()[0].body).toBeNull()
    expect(screen.queryByRole('button', { name: 'Mark concluded Name of cmp_D' })).toBeNull()
    expect(watch.texts.slice(-2)).toEqual(['Marking concluded…', 'Marked concluded'])
    // The campaign is still selectable and nothing about the selection changed.
    expect(live.c.selection).toEqual({ kind: 'none' })
  })

  it('S-9 the pending button is aria-disabled and stays mounted through its request, and a second press is no second POST', async () => {
    let held: Call | null = null
    const { server } = await mount({
      route: (call) => {
        if (call.method === 'POST') {
          held = call
          return 'defer'
        }
        return withConcluded()(call)
      },
    })
    openTavern()
    const button = await screen.findByRole('button', { name: 'Mark concluded Name of cmp_D' })
    await userEvent.click(button)
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).toBeInTheDocument()
    await userEvent.click(button)
    expect(server.posts()).toHaveLength(1)
    act(() => held?.reply({ status: 200, body: campaignBody('cmp_D', { concluded_at: day(12) }) }))
    await screen.findByRole('button', { name: 'Concluded (3)' })
  })

  it('S-10 a 503 keeps the card, keeps focus on the button and announces the failure', async () => {
    const { server } = await mount({ route: concludedRoute(() => ({ status: 503 })) })
    openTavern()
    const button = await screen.findByRole('button', { name: 'Mark concluded Name of cmp_D' })
    await userEvent.click(button)
    await waitFor(() => expect(statusNode()).toHaveTextContent("Couldn't change the campaign. Try again."))
    expect(button).toBeInTheDocument()
    expect(button).toHaveFocus()
    expect(button).not.toHaveAttribute('aria-disabled')
    expect(screen.getByRole('button', { name: 'Concluded (2)' })).toBeInTheDocument()
    expect(server.campaignLines().filter((l) => l.startsWith('GET'))).toEqual(['GET /campaigns'])
  })

  it('S-10 a 404 re-reads the list once and says the campaign is no longer available', async () => {
    const { server } = await mount({ route: concludedRoute(() => ({ status: 404 })) })
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Mark concluded Name of cmp_D' }))
    await waitFor(() => expect(statusNode()).toHaveTextContent('That campaign is no longer available'))
    await waitFor(() => expect(server.lines().filter((l) => l === 'GET /campaigns')).toHaveLength(2))
    await act(async () => {})
    expect(server.lines().filter((l) => l === 'GET /campaigns')).toHaveLength(2)
    // The re-read announced nothing of its own over the note.
    expect(statusNode()).toHaveTextContent('That campaign is no longer available')
  })

  it('S-11 Reopen moves the card back to the active list and focuses its Prep', async () => {
    const { server } = await mount({ route: concludedRoute() })
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Concluded (2)' }))
    await userEvent.click(screen.getByRole('button', { name: 'Reopen Name of cmp_C1' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Concluded (1)' })).toBeInTheDocument())
    const activeList = screen.getByRole('list', { name: 'Your campaigns' })
    expect(within(activeList).getByRole('button', { name: 'Prep Name of cmp_C1' })).toHaveFocus()
    expect(within(activeList).getByRole('button', { name: 'Start Session Name of cmp_C1' })).toBeInTheDocument()
    expect(server.posts().map((c) => c.url)).toEqual(['/campaigns/cmp_C1/reopen'])
    await waitFor(() => expect(statusNode()).toHaveTextContent('Reopened'))
  })

  it('S-11 a reopened campaign that sorts past the shown cards reveals its page and takes focus', async () => {
    const twelve = Array.from({ length: 12 }, (_, i) => campaignBody(`cmp_f${String(i + 1).padStart(2, '0')}`, {
      last_activity_at: day(28 - i),
    }))
    const old = campaignBody('cmp_old', { concluded_at: day(10), last_activity_at: day(1) })
    const route: Route = (call) => {
      if (call.method === 'POST') return { status: 200, body: campaignBody('cmp_old', { last_activity_at: day(1), concluded_at: null }) }
      return listOf([...twelve, old])(call)
    }
    await mount({ route })
    openTavern()
    await findPrep('cmp_f12')
    await userEvent.click(await screen.findByRole('button', { name: 'Concluded (1)' }))
    await userEvent.click(screen.getByRole('button', { name: 'Reopen Name of cmp_old' }))
    expect(await findPrep('cmp_old')).toHaveFocus()
    expect(screen.getAllByRole('button', { name: /^Prep / })).toHaveLength(13)
  })

  it('S-11 reopening the last concluded campaign removes the toggle and still lands focus on the card', async () => {
    const route: Route = (call) => {
      if (call.method === 'POST') return { status: 200, body: campaignBody('cmp_C1', { last_activity_at: day(7), concluded_at: null }) }
      return listOf([campaignBody('cmp_C1', { concluded_at: day(10), last_activity_at: day(7) })])(call)
    }
    await mount({ route })
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Concluded (1)' }))
    await userEvent.click(screen.getByRole('button', { name: 'Reopen Name of cmp_C1' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Concluded/ })).toBeNull())
    expect(await findPrep('cmp_C1')).toHaveFocus()
  })
})

describe('Prep and Start Session (S-12)', () => {
  it('S-12 Prep makes no POST, selects the campaign, closes the open conversation and lands in the GM workspace (R-1)', async () => {
    const { server } = await mount({ route: listOf([campaignBody('cmp_A')]) })
    act(() => { live.raw.setConversationId('cnv_legacy') })
    openTavern()
    await userEvent.click(await findPrep('cmp_A'))
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    expect(live.c.selection).toMatchObject({ campaign: { campaign_id: 'cmp_A' } })
    expect(live.raw.conversationId).toBeNull()
    expect(live.nav.screen).toBe('workspace')
    expect(live.nav.mode).toBe('gm')
    expect(server.posts()).toHaveLength(0)
  })

  it('Start Session is locked: focusable, aria-disabled, described by the Loremaster line, and a press does nothing', async () => {
    const { server } = await mount({ route: listOf([campaignBody('cmp_A')]) })
    openTavern()
    await findPrep('cmp_A')
    const start = screen.getByRole('button', { name: 'Start Session Name of cmp_A' })
    expect(start).toHaveAttribute('aria-disabled', 'true')
    expect(start).not.toBeDisabled()
    expect(start).toHaveAccessibleDescription('Running a live table is part of Loremaster')
    const before = server.calls.length
    const screenBefore = live.nav.screen
    await userEvent.click(start)
    start.focus()
    await userEvent.keyboard('{Enter}')
    expect(server.calls).toHaveLength(before)
    expect(live.nav.screen).toBe(screenBefore)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('Where you were, and the empty states (S-13, S-14)', () => {
  const seedSage = (store: MemoryConversationStore): void => {
    store.create('gm', 'a gm thread that is never offered')
    const sage = store.create('sage', 'What is a fireball?')
    store.recordFirstPrompt(sage.id, 'What is a fireball?')
  }

  it('S-13 offers the latest non-GM conversation, and Back to that thread opens that mode and id', async () => {
    const { store } = await mount({ seed: seedSage })
    openTavern()
    expect(await screen.findByRole('heading', { level: 2, name: 'Where you were' })).toBeInTheDocument()
    expect(screen.getByText('Sage · “What is a fireball?”')).toBeInTheDocument()
    // The §12.2 empty line and Begin anew are below it.
    expect(screen.getByText('Create your first campaign — only a name is required')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Begin anew' })).toBeInTheDocument()
    const sage = store.list('sage')[0]
    await userEvent.click(screen.getByRole('button', { name: 'Back to that thread' }))
    expect(live.nav.screen).toBe('workspace')
    expect(live.nav.mode).toBe('sage')
    expect(live.raw.conversationId).toBe(sage.id)
  })

  it('S-13 is absent once any owned campaign exists, and absent with no conversations (controls)', async () => {
    const owned = await mount({ seed: seedSage, route: listOf([campaignBody('cmp_A')]) })
    openTavern()
    await findPrep('cmp_A')
    expect(screen.queryByRole('heading', { name: 'Where you were' })).toBeNull()
    owned.view.unmount()
    await mount()
    openTavern()
    await screen.findByText('Create your first campaign', { exact: false })
    expect(screen.queryByRole('heading', { name: 'Where you were' })).toBeNull()
  })

  it('S-14 the brand-new state shows the empty line; a concluded-only account does not, and nothing is focused for it', async () => {
    await mount()
    openTavern()
    expect(await screen.findByText('Create your first campaign — only a name is required')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Begin anew' })).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Your campaigns' })).toBeNull()
  })

  it('S-14 concluded only: no empty line, Begin anew and the toggle are there, and focus stays on the h1', async () => {
    await mount({ route: listOf([campaignBody('cmp_C1', { concluded_at: day(10) })]) })
    openTavern()
    await screen.findByRole('button', { name: 'Concluded (1)' })
    await act(async () => {})
    expect(screen.queryByText('Create your first campaign', { exact: false })).toBeNull()
    expect(screen.getByRole('heading', { level: 2, name: 'Begin anew' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveFocus()
  })
})

describe('errors, creation and storage (S-15, S-17, S-18)', () => {
  it('S-15 a failed re-read keeps the cards already shown (STATE-1); Retry stays mounted while busy and focus is never dropped', async () => {
    let failing = false
    const route: Route = (call) => {
      if (call.method === 'GET' && call.url === '/campaigns') {
        if (failing) return { status: 503 }
        return { status: 200, body: page([campaignBody('cmp_A')]) }
      }
      return defaultRoute(call)
    }
    const { server } = await mount({ route })
    openTavern()
    await findPrep('cmp_A')
    await userEvent.click(screen.getByRole('button', { name: 'Back to chat' }))
    failing = true
    openTavern()
    const retry = await screen.findByRole('button', { name: 'Retry' })
    expect(prepButton('cmp_A')).toBeInTheDocument()
    expect(screen.getByText("Couldn't load campaigns", { selector: 'p:not([role])' })).toBeInTheDocument()
    expect(statusNode()).toHaveTextContent("Couldn't load campaigns")
    failing = false
    server.calls.length = 0
    await userEvent.click(retry)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull())
    expect(server.lines()).toEqual(['GET /campaigns'])
    expect(document.activeElement).not.toBe(document.body)
  })

  it('S-15 Retry stays mounted and aria-disabled through its own request', async () => {
    let n = 0
    const { server } = await mount({
      route: (call) => (call.url === '/campaigns' ? (n++ === 0 ? { status: 503 } : 'defer') : defaultRoute(call)),
    })
    openTavern()
    const retry = await screen.findByRole('button', { name: 'Retry' })
    await userEvent.click(retry)
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveAttribute('aria-disabled', 'true')
    act(() => server.calls[server.calls.length - 1].reply({ status: 200, body: EMPTY_PAGE }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull())
  })

  it('Begin anew creates a campaign and lands in its GM channel (74j R-1), announcing the create', async () => {
    const route: Route = (call) => (call.method === 'POST' && call.url === '/campaigns'
      ? { status: 201, body: campaignBody('cmp_new', { name: 'The Hollow Crown' }) }
      : defaultRoute(call))
    const { server } = await mount({ route })
    act(() => { live.raw.setConversationId('cnv_legacy') })
    openTavern()
    await screen.findByText('Create your first campaign', { exact: false })
    await userEvent.type(screen.getByRole('textbox', { name: 'Campaign name' }), 'The Hollow Crown')
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(live.nav.screen).toBe('workspace'))
    expect(live.nav.mode).toBe('gm')
    expect(live.raw.conversationId).toBeNull()
    expect(live.c.selection).toMatchObject({ kind: 'selected', campaign: { campaign_id: 'cmp_new' } })
    expect(server.posts().map((c) => c.url)).toEqual(['/campaigns'])
  })

  it('S-17 no campaign id, name or typed name reaches web storage or the page title', async () => {
    const title = document.title
    const typed = 'Typed Secret Name'
    const secrets = ['cmp_A', 'Name of cmp_A', 'cmp_D', 'Name of cmp_D', typed]
    const held = (secret: string): boolean => {
      for (const area of [window.localStorage, window.sessionStorage]) {
        for (let i = 0; i < area.length; i += 1) {
          const key = area.key(i) as string
          if (key.includes(secret) || (area.getItem(key) ?? '').includes(secret)) return true
        }
      }
      return false
    }
    // The helper can see a planted value (a control).
    window.sessionStorage.setItem('probe', 'cmp_A')
    expect(held('cmp_A')).toBe(true)
    window.sessionStorage.removeItem('probe')
    const route: Route = (call) => {
      if (call.method === 'POST' && call.url === '/campaigns/cmp_D/conclude') {
        return { status: 200, body: campaignBody('cmp_D', { dormant: true, concluded_at: day(12) }) }
      }
      return listOf([campaignBody('cmp_A'), campaignBody('cmp_D', { dormant: true, last_activity_at: day(1) })])(call)
    }
    await mount({ route })
    openTavern()
    await findPrep('cmp_A')
    await userEvent.type(screen.getByRole('textbox', { name: 'Campaign name' }), typed)
    await userEvent.click(screen.getByRole('button', { name: 'Mark concluded Name of cmp_D' }))
    await screen.findByRole('button', { name: 'Concluded (1)' })
    await userEvent.click(screen.getByRole('button', { name: 'Concluded (1)' }))
    for (const secret of secrets) expect(held(secret), secret).toBe(false)
    expect(document.title).toBe(title)
  })

  it('S-18 Back to chat returns to the workspace with the campaign unchanged', async () => {
    await mount({ campaign: 'cmp_A', route: listOf([campaignBody('cmp_A')]) })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Back to chat' }))
    expect(live.nav.screen).toBe('workspace')
    expect(live.c.selection).toMatchObject({ kind: 'selected', campaign: { campaign_id: 'cmp_A' } })
  })

  it('a player is no longer bounced (30c PR-2): the tavern renders and no campaign request is made', async () => {
    const { server } = await mount({ role: 'player', initialScreen: 'tavern' })
    expect(await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeInTheDocument()
    expect(live.nav.screen).toBe('tavern')
    await waitFor(() => expect(server.lines()).toEqual(['GET /seats']))
  })
})

describe('navigation on selection change (74j, T-9, T-10, T-12a mutation coverage)', () => {
  it('a pick (Prep) nulls the open conversation and enters the GM workspace (R-1)', async () => {
    await mount({ route: listOf([campaignBody('cmp_A')]) })
    act(() => { live.raw.setConversationId('cnv_legacy') })
    act(() => live.nav.openTavern?.())
    await userEvent.click(await findPrep('cmp_A'))
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

// ── 30c PR-2: the player's tavern and "Your seats" ────────────────────────────

describe('the player tavern and Your seats (30c PR-2, SP-1 to SP-13)', () => {
  const asPlayer = (route: Route = defaultRoute, seed?: (store: MemoryConversationStore) => void) =>
    mount({ role: 'player', initialScreen: 'tavern', route, seed })
  const seatNames = (): string[] => within(screen.getByRole('list', { name: 'Your seats' }))
    .getAllByRole('heading', { level: 2 }).map((h) => h.textContent ?? '')
  const seatCard = (id: string): HTMLElement => screen.getByRole('group', { name: `Table of ${id}` })
  const NOTE = 'Creating campaigns is not available to your account yet'

  it('SP-1 a player gets the screen: one h1 and one status, no campaign request, no Begin anew, a locked New Campaign with its reason', async () => {
    const { server } = await asPlayer()
    expect(await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeInTheDocument()
    await screen.findByRole('heading', { level: 2, name: 'Campaigns are being built' })
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(server.calls.some((c) => c.url.startsWith('/campaigns'))).toBe(false)
    expect(screen.queryByRole('heading', { name: 'Begin anew' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Continue without a campaign' })).toBeNull()
    const locked = screen.getByRole('button', { name: 'New Campaign' })
    expect(locked).toHaveAttribute('aria-disabled', 'true')
    expect(locked).toHaveAccessibleDescription(NOTE)
    await userEvent.click(locked)
    expect(live.nav.screen).toBe('tavern')
    expect(server.lines()).toEqual(['GET /seats'])
    // Control: a dm's New Campaign is a live button with no such reason.
    cleanup()
    await mount({ initialScreen: 'tavern' })
    const open = await screen.findByRole('button', { name: 'New Campaign' })
    expect(open).not.toHaveAttribute('aria-disabled')
  })

  it('SP-2 the seats are ordered live first, concluded last, each by when the table last met (else when the seat was taken)', async () => {
    const seats = [
      seatBody('cmp_A', { last_played_at: day(5) }),
      seatBody('cmp_C', { concluded: true, last_played_at: day(28) }),
      seatBody('cmp_B', { live: true, accepted_at: day(1) }),
      seatBody('cmp_D', { confirmed: false, live: true, accepted_at: day(9) }),
    ]
    await asPlayer(seatedWith(seats))
    await screen.findByRole('list', { name: 'Your seats' })
    expect(seatNames()).toEqual(['Table of cmp_B', 'Table of cmp_D', 'Table of cmp_A', 'Table of cmp_C'])
    // Fed the other way round, the order is the same: it is the client's.
    cleanup()
    await asPlayer(seatedWith([...seats].reverse()))
    await screen.findByRole('list', { name: 'Your seats' })
    expect(seatNames()).toEqual(['Table of cmp_B', 'Table of cmp_D', 'Table of cmp_A', 'Table of cmp_C'])
  })

  it('SP-3 each seat says one thing about its standing: plain "Live now" and no link or button, waiting, concluded, or last played', async () => {
    const seats = [
      seatBody('cmp_B', { live: true, tone: 'Mystery · Low magic' }),
      seatBody('cmp_D', { confirmed: false, live: true }),
      seatBody('cmp_C', { concluded: true }),
      seatBody('cmp_A', { last_played_at: '2026-09-04T12:00:00Z', alias: 'Ysolde' }),
    ]
    await asPlayer(seatedWith(seats))
    await screen.findByRole('list', { name: 'Your seats' })

    const live = within(seatCard('cmp_B'))
    expect(live.getByText('Live now')).toBeInTheDocument()
    expect(live.getByText('Mystery · Low magic')).toBeInTheDocument()
    expect(live.getByText('Playing as Brannoc')).toBeInTheDocument()
    expect(live.getByText('5e')).toBeInTheDocument()
    expect(live.queryByText('Last played', { exact: false })).toBeNull()
    // ID-17: no table page exists, so there is nothing to press and nowhere to go.
    expect(screen.queryAllByRole('link')).toHaveLength(0)
    expect(document.querySelectorAll('a')).toHaveLength(0)
    for (const id of ['cmp_B', 'cmp_D', 'cmp_C', 'cmp_A']) {
      expect(within(seatCard(id)).queryAllByRole('button'), id).toHaveLength(0)
    }

    // An unconfirmed seat cannot join, whatever the table is doing (D-12).
    const waiting = within(seatCard('cmp_D'))
    expect(waiting.getByText('Waiting for your GM to confirm your seat')).toBeInTheDocument()
    expect(waiting.queryByText('Live now')).toBeNull()
    expect(within(seatCard('cmp_C')).getByText('This table has concluded')).toBeInTheDocument()
    expect(within(seatCard('cmp_C')).queryByText('Live now')).toBeNull()
    const quiet = within(seatCard('cmp_A'))
    expect(quiet.getByText(/^Last played 4 September( 2026)?$/)).toBeInTheDocument()
    expect(quiet.getByText('Playing as Ysolde')).toBeInTheDocument()
    expect(quiet.queryByText('Live now')).toBeNull()
    expect(screen.getAllByText('Live now')).toHaveLength(1)
  })

  it('SP-4 a player with no seat gets E-1, and Ask the Sage enters the workspace in Sage; a player with a seat does not', async () => {
    await asPlayer()
    await screen.findByRole('heading', { level: 2, name: 'Campaigns are being built' })
    expect(screen.getByText('Prep, documents and the live table are coming.', { exact: false })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Tell me when campaigns open' })).toBeNull()
    expect(screen.queryByRole('list', { name: 'Your seats' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Ask the Sage' }))
    expect(live.nav.screen).toBe('workspace')
    expect(live.nav.mode).toBe('sage')
    cleanup()
    await asPlayer(seatedWith([seatBody('cmp_A')]))
    await screen.findByRole('list', { name: 'Your seats' })
    expect(screen.queryByRole('heading', { name: 'Campaigns are being built' })).toBeNull()
  })

  it('SP-4 E-1 is never shown while the seats load or after they fail (an empty guess would be untrue)', async () => {
    const held = await asPlayer((call) => (call.url.startsWith('/seats') ? 'defer' : defaultRoute(call)))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(screen.queryByRole('heading', { name: 'Campaigns are being built' })).toBeNull()
    expect(screen.getByText('Loading your seats…', { selector: 'p.tavern-screen__message' })).toBeInTheDocument()
    expect(document.querySelectorAll('.tavern-screen__skeleton')).toHaveLength(2)
    held.view.unmount()
    cleanup()
    await asPlayer((call) => (call.url.startsWith('/seats') ? { status: 503 } : defaultRoute(call)))
    await screen.findByRole('button', { name: 'Retry' })
    expect(screen.queryByRole('heading', { name: 'Campaigns are being built' })).toBeNull()
  })

  it('SP-5 "Where you were" shows for a player with no seat and a started thread, and is absent once there is a seat', async () => {
    const seed = (store: MemoryConversationStore): void => {
      const sage = store.create('sage', 'What is a fireball?')
      store.recordFirstPrompt(sage.id, 'What is a fireball?')
    }
    const { store } = await asPlayer(defaultRoute, seed)
    expect(await screen.findByRole('heading', { level: 2, name: 'Where you were' })).toBeInTheDocument()
    expect(screen.getByText('Sage · “What is a fireball?”')).toBeInTheDocument()
    const sage = store.list('sage')[0]
    await userEvent.click(screen.getByRole('button', { name: 'Back to that thread' }))
    expect(live.nav.mode).toBe('sage')
    expect(live.raw.conversationId).toBe(sage.id)
    cleanup()
    await asPlayer(seatedWith([seatBody('cmp_A')]), seed)
    await screen.findByRole('list', { name: 'Your seats' })
    expect(screen.queryByRole('heading', { name: 'Where you were' })).toBeNull()
  })

  it('SP-6 a player hears "Loading your seats…" once and "Seats loaded" once, in the one status node', async () => {
    const watch = watchStatus()
    await asPlayer(seatedWith([seatBody('cmp_A')]))
    await screen.findByRole('list', { name: 'Your seats' })
    await waitFor(() => expect(statusNode()).toHaveTextContent('Seats loaded'))
    watch.stop()
    expect(watch.texts).toEqual(['Loading your seats…', 'Seats loaded'])
    expect(live.statusAtMount).toBe('')
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('SP-7 follows the cursor on its own, and stops at exactly TAVERN_MAX_PAGES reads however long the server goes on', async () => {
    const twoPages: Route = (call) => {
      if (call.method === 'GET' && call.url === '/seats') return { status: 200, body: page([seatBody('cmp_A')], 's2') }
      if (call.method === 'GET' && call.url === '/seats?cursor=s2') return { status: 200, body: page([seatBody('cmp_B')]) }
      return defaultRoute(call)
    }
    const two = await asPlayer(twoPages)
    await screen.findByRole('list', { name: 'Your seats' })
    await waitFor(() => expect(seatNames()).toHaveLength(2))
    expect(two.server.lines()).toEqual(['GET /seats', 'GET /seats?cursor=s2'])
    two.view.unmount()
    cleanup()

    const endless: Route = (call) => {
      const read = /^\/seats(?:\?cursor=s(\d+))?$/.exec(call.url)
      if (call.method === 'GET' && read !== null) {
        const n = read[1] === undefined ? 0 : Number(read[1])
        return { status: 200, body: page([seatBody(`cmp_p${n}`)], `s${n + 1}`) }
      }
      return defaultRoute(call)
    }
    const many = await asPlayer(endless)
    await waitFor(() => expect(statusNode()).toHaveTextContent('Seats loaded'))
    await act(async () => {})
    expect(many.server.lines()).toHaveLength(TAVERN_MAX_PAGES)
    expect(seatNames()).toHaveLength(TAVERN_MAX_PAGES)
  })

  it('SP-8 a failed read says so in the status and a plain line; one Retry asks again, and its success lands focus on the h1', async () => {
    let failing = true
    const route: Route = (call) => (call.url.startsWith('/seats')
      ? (failing ? { status: 503 } : { status: 200, body: page([seatBody('cmp_A')]) })
      : defaultRoute(call))
    const { server } = await asPlayer(route)
    const retry = await screen.findByRole('button', { name: 'Retry' })
    expect(statusNode()).toHaveTextContent("Couldn't load your seats")
    expect(document.querySelector('p.tavern-screen__message')).toHaveTextContent("Couldn't load your seats")
    expect(screen.getAllByRole('button', { name: /^Retry/ })).toHaveLength(1)
    failing = false
    await userEvent.click(retry)
    await screen.findByRole('list', { name: 'Your seats' })
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveFocus())
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(server.lines()).toEqual(['GET /seats', 'GET /seats'])
    expect(statusNode()).toHaveTextContent('Seats loaded')
  })

  it('SP-9 a dm sees its campaigns and its seats together, and the end message waits for both reads', async () => {
    const watch = watchStatus()
    let held: Call | null = null
    const route: Route = (call) => {
      if (call.url.startsWith('/seats')) {
        held = call
        return 'defer'
      }
      return listOf([campaignBody('cmp_A')])(call)
    }
    await mount({ route })
    openTavern()
    await findPrep('cmp_A')
    expect(statusNode()).toHaveTextContent('Loading campaigns…')
    expect(screen.queryByRole('list', { name: 'Your seats' })).toBeNull()
    act(() => (held as Call | null)?.reply({ status: 200, body: page([seatBody('cmp_S')]) }))
    await screen.findByRole('list', { name: 'Your seats' })
    await waitFor(() => expect(statusNode()).toHaveTextContent('Campaigns loaded'))
    watch.stop()
    expect(watch.texts).toEqual(['Loading campaigns…', 'Campaigns loaded'])
    expect(screen.getByRole('heading', { level: 2, name: 'Begin anew' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Your campaigns' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Your seats' })).toBeInTheDocument()
  })

  it('SP-9 a dm whose seats fail keeps its campaigns, hears it once, and has ONE Retry that re-asks only the seats', async () => {
    let failing = true
    const route: Route = (call) => (call.url.startsWith('/seats')
      ? (failing ? { status: 503 } : { status: 200, body: page([seatBody('cmp_S')]) })
      : listOf([campaignBody('cmp_A')])(call))
    const { server } = await mount({ route })
    openTavern()
    await findPrep('cmp_A')
    const retry = await screen.findByRole('button', { name: 'Retry' })
    expect(screen.getAllByRole('button', { name: /^Retry/ })).toHaveLength(1)
    expect(statusNode()).toHaveTextContent("Couldn't load your seats")
    const lines = Array.from(document.querySelectorAll('p.tavern-screen__message')).map((p) => p.textContent)
    expect(lines).toEqual(["Couldn't load your seats"])
    expect(prepButton('cmp_A')).toBeInTheDocument()
    failing = false
    await userEvent.click(retry)
    await screen.findByRole('list', { name: 'Your seats' })
    expect(server.campaignLines()).toEqual(['GET /campaigns'])
    expect(server.lines().filter((l) => l.startsWith('GET /seats'))).toHaveLength(2)
    await waitFor(() => expect(statusNode()).toHaveTextContent('Campaigns loaded'))
  })

  it('SP-9 both reads failing is one Retry and both lines; pressing it re-asks both', async () => {
    const route: Route = (call) => (call.url.startsWith('/seats') || call.url === '/campaigns' ? { status: 503 } : defaultRoute(call))
    const { server } = await mount({ route })
    openTavern()
    const retry = await screen.findByRole('button', { name: 'Retry' })
    expect(screen.getAllByRole('button', { name: /^Retry/ })).toHaveLength(1)
    const lines = () => Array.from(document.querySelectorAll('p.tavern-screen__message')).map((p) => p.textContent)
    expect(lines()).toEqual(["Couldn't load campaigns", "Couldn't load your seats"])
    // The campaign failure is the one announced when both fail.
    expect(statusNode()).toHaveTextContent("Couldn't load campaigns")
    await userEvent.click(retry)
    await waitFor(() => expect(server.lines().filter((l) => l === 'GET /campaigns')).toHaveLength(2))
    await waitFor(() => expect(server.lines().filter((l) => l === 'GET /seats')).toHaveLength(2))
  })

  it('SP-10 more than 12 seats show 12, and Show more seats reveals the rest with no request; 12 show no button', async () => {
    const thirteen = Array.from({ length: 13 }, (_, i) => seatBody(`cmp_s${String(i).padStart(2, '0')}`, { last_played_at: day(i + 1) }))
    const { server } = await asPlayer(seatedWith(thirteen))
    await screen.findByRole('list', { name: 'Your seats' })
    expect(seatNames()).toHaveLength(12)
    const before = server.calls.length
    await userEvent.click(screen.getByRole('button', { name: 'Show more seats' }))
    expect(seatNames()).toHaveLength(13)
    expect(screen.queryByRole('button', { name: 'Show more seats' })).toBeNull()
    expect(server.calls.length).toBe(before)
    cleanup()
    await asPlayer(seatedWith(thirteen.slice(0, 12)))
    await screen.findByRole('list', { name: 'Your seats' })
    expect(seatNames()).toHaveLength(12)
    expect(screen.queryByRole('button', { name: 'Show more seats' })).toBeNull()
  })

  it('SP-11 a seat card is a non-interactive group under a Your seats heading, and the player has its own subhead', async () => {
    await asPlayer(seatedWith([seatBody('cmp_A')]))
    const card = await screen.findByRole('group', { name: 'Table of cmp_A' })
    expect(card).not.toHaveAttribute('tabindex')
    expect(card.className).not.toContain('card--interactive')
    expect(screen.getByRole('heading', { level: 2, name: 'Your seats' })).toBeInTheDocument()
    expect(screen.getByText('The tables where you have a seat.')).toBeInTheDocument()
  })

  it('SP-12 no seat name, alias or tone reaches web storage, the URL or the page title', async () => {
    const title = document.title
    const seat = seatBody('cmp_secret1', { campaign_name: 'Wyrmkeep Obsidian', alias: 'Qarth Vellum', tone: 'Grimdark dread' })
    await asPlayer(seatedWith([seat]))
    await screen.findByRole('group', { name: 'Wyrmkeep Obsidian' })
    const stored = [window.localStorage, window.sessionStorage].flatMap((area) => (
      Array.from({ length: area.length }, (_, i) => area.key(i) ?? '').flatMap((k) => [k, area.getItem(k) ?? ''])
    )).join('\n')
    for (const secret of ['cmp_secret1', 'Wyrmkeep', 'Obsidian', 'Qarth', 'Vellum', 'Grimdark']) {
      expect(stored.includes(secret), secret).toBe(false)
    }
    expect(document.title).toBe(title)
    expect(window.location.search).toBe('')
    expect(window.location.hash).toBe('')
  })

  it('SP-13 a role change at the tavern keeps the screen: the dm cards go and the player view comes, with a fresh seat read', async () => {
    const { server } = await mount({ route: listOf([campaignBody('cmp_A')]) })
    openTavern()
    await findPrep('cmp_A')
    const seatReads = (): number => server.lines().filter((l) => l === 'GET /seats').length
    expect(seatReads()).toBe(1)
    act(() => { live.user.signIn({ email: 'ada@example.com', role: 'player' }) })
    await screen.findByRole('heading', { level: 2, name: 'Campaigns are being built' })
    expect(live.nav.screen).toBe('tavern')
    expect(screen.queryByRole('button', { name: 'Prep Name of cmp_A' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Begin anew' })).toBeNull()
    expect(seatReads()).toBe(2)
  })
})
