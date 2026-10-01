/**
 * tavernRouting.test.tsx -- the /tavern screen in the app that ships
 * (agent-forge-harness-74j, brief section 7, T-4 to T-29; review pr226-o and
 * its rework plan pr226-plan1, Critic 44, 45, 47, 48, 49).
 *
 * Mounts `AppRoot` -- the component `main.tsx` renders, as
 * `campaignRouting.test.tsx` does -- with `fetch` replaced by a recorder, so
 * every claim about which request is made, and which never is, is about the
 * real provider tower. `bootProbed` mounts the same tower through `ProbedRoot`,
 * which additionally exposes the raw (pre-CampaignProvider) and scoped AppNav,
 * the campaign context and the current user through `live`, for assertions and
 * actions (`openTavern`, `registerSwitchGuard`, `signOut`) a real user gesture
 * has no other way to reach deterministically.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import { ThemeProvider } from './ds/theme'
import { AppNavContext, AppNavProvider, useAppNav, type AppNavState } from './shell/AppNav'
import { AppRoot } from './AppRoot'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './shell/campaignContext'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './shell/currentUser'
import { ConversationStoreProvider } from './shell/ConversationStoreContext'
import { UrlNavigation } from './shell/UrlNavigation'
import { startScreen } from './shell/routes'
import { readCampaignRestore } from './shell/workspaceFragment'
import * as api from './api'

function campaignBody(id: string) {
  return {
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
  }
}
const page = (items: unknown[]) => ({ schema_version: 1, items, next_cursor: null })

/** `campaignRouting.test.tsx`'s `THREAD`, with a conversation id of its own so a
 * sweep failure can tell this file's server answers from that one's. */
const THREAD_T = {
  schema_version: 1, conversation_id: 'cnv_T', campaign_id: 'cmp_A', title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
}

function bodyId(body: string | null): string {
  return /"conversation_id":"(\w+)"/.exec(body ?? '')?.[1] ?? /"campaign_id":"(\w+)"/.exec(body ?? '')?.[1] ?? 'cnv_1'
}

// ── The §T server (Critic 45, 48, 49) ───────────────────────────────────────

type Reply = { status: number; body?: unknown } | 'network'
interface Call { url: string; method: string; body: string | null; headers: Headers; signal: AbortSignal | null; reply: (r: Reply) => void }
type Route = (call: Call) => Reply | 'defer'

/** LeftNavCampaign.test.tsx:50-66, plus the request headers (Critic 48). */
function stubServer(route: Route) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null,
      headers: new Headers(init?.headers), signal: init?.signal ?? null,
      reply: (r) => (r === 'network' ? reject(new TypeError('Failed to fetch'))
        : resolve(new Response(JSON.stringify(r.body ?? {}), { status: r.status }))),
    }
    calls.push(call)
    call.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')))
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  const lines = () => calls.map((c) => `${c.method} ${c.url}`)
  return { fetchImpl, calls, lines, scoped: () => lines().filter((l) => / \/(campaigns|conversations)/.test(l)) }
}

/** The default route: §T's table. `POST /campaigns` echoes the posted name;
 * `POST /conversations` echoes the posted campaign id. */
const defaultRoute: Route = ({ method, url, body }) => {
  if (method === 'GET' && url === '/campaigns') return { status: 200, body: page([campaignBody('cmp_A')]) }
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (method === 'GET' && one !== null) return { status: 200, body: campaignBody(one[1]) }
  if (method === 'GET' && url === '/conversations/cnv_T') return { status: 200, body: THREAD_T }
  if (method === 'GET' && /^\/conversations\?campaign_id=/.test(url)) return { status: 200, body: page([]) }
  if (method === 'POST' && url === '/campaigns') {
    const posted = body === null ? {} : (JSON.parse(body) as { name?: string })
    return { status: 201, body: { ...campaignBody('cmp_New'), name: posted.name ?? 'cmp_New' } }
  }
  if (method === 'POST' && url === '/conversations') {
    const posted = body === null ? {} : (JSON.parse(body) as { campaign_id?: string })
    return { status: 201, body: { ...THREAD_T, conversation_id: 'cnv_new', campaign_id: posted.campaign_id ?? 'cmp_A' } }
  }
  if (method === 'POST' && url === '/chat') {
    return { status: 200, body: { answer: 'ok', sources: [], answerable: true, conversation_id: bodyId(body) } }
  }
  return { status: 404, body: {} }
}

/** A campaign list of exactly `items`; everything else is `defaultRoute`. */
const listOf = (items: unknown[]): Route => (c) => (c.method === 'GET' && c.url === '/campaigns' ? { status: 200, body: page(items) } : defaultRoute(c))

// ── Booting the real tower ──────────────────────────────────────────────────

interface BootOptions { role?: 'dm' | 'player'; signedOut?: boolean; strict?: boolean; route?: Route }

function mount(url: string, options: BootOptions, Root: React.ComponentType) {
  const server = stubServer(options.route ?? defaultRoute)
  vi.stubGlobal('fetch', server.fetchImpl)
  vi.spyOn(api, 'getMe').mockResolvedValue(options.signedOut === true
    ? { kind: 'error', status: 401, message: 'not signed in' }
    : { kind: 'ok', user: { email: 'ada@example.com', role: options.role ?? 'dm' } })
  window.history.replaceState(null, '', url)
  render(options.strict === true ? <React.StrictMode><Root /></React.StrictMode> : <Root />)
  return server
}

/** Stub `fetch` and `getMe`, put `url` in the address bar and mount the SAME
 * `AppRoot` `main.tsx` renders. */
function boot(url: string, options: BootOptions = {}) {
  return mount(url, options, AppRoot)
}

/** The same boot, through `ProbedRoot` -- `live` is populated after the first
 * commit. */
function bootProbed(url: string, options: BootOptions = {}) {
  return mount(url, options, ProbedRoot)
}

const live = {} as { raw: AppNavState; nav: AppNavState; c: CampaignContextValue; user: CurrentUserContextValue }
function RawProbe(): null {
  const raw = React.useContext(AppNavContext)
  React.useLayoutEffect(() => { live.raw = raw })
  return null
}
function ScopedProbe(): null {
  const nav = useAppNav(); const c = useCampaign(); const user = useCurrentUser()
  React.useLayoutEffect(() => { live.nav = nav; live.c = c; live.user = user })
  return null
}
/** AppRoot.tsx:19-48's tower and boot computation, with R-5's raw probe between
 * the conversation store and the campaign provider. Renders no DOM of its own. */
function ProbedRoot(): React.JSX.Element {
  const [boot] = React.useState(() => ({
    route: startScreen(window.location.pathname),
    restore: readCampaignRestore(window.location.pathname, window.location.hash),
  }))
  return (
    <ThemeProvider>
      <AppNavProvider initialScreen={boot.restore === null ? boot.route.screen : 'workspace'} initialMode={boot.restore === null ? 'sage' : 'gm'}>
        <CurrentUserProvider>
          <ConversationStoreProvider>
            <RawProbe />
            <CampaignProvider restore={boot.restore}>
              <UrlNavigation />
              <App />
              <ScopedProbe />
            </CampaignProvider>
          </ConversationStoreProvider>
        </CurrentUserProvider>
      </AppNavProvider>
    </ThemeProvider>
  )
}

/** Call this from `live.nav`/`live.raw` -- both expose the same action. */
function openTavern(): void {
  act(() => { live.nav.openTavern?.() })
}

/** Critic 14 and 45: any window of min(8, length) characters of `text`, in any
 * web-storage key or value. */
function storageHolds(text: string): boolean {
  const size = Math.min(8, text.length)
  const stored: string[] = []
  for (const area of [localStorage, sessionStorage]) {
    for (let i = 0; i < area.length; i += 1) { const key = area.key(i); if (key !== null) stored.push(key, area.getItem(key) ?? '') }
  }
  const all = stored.join('\n')
  for (let i = 0; i + size <= text.length; i += 1) if (all.includes(text.slice(i, i + size))) return true
  return false
}

/** Critic 34 and 49: start it BEFORE the render, press or sign-in it judges.
 * Counts mutation batches, batches that added a node holding `text`, and
 * batches after which `text` is on screen. */
function watch(text: string): () => { batches: number; added: number; present: number } {
  let batches = 0; let added = 0; let present = 0
  const scan = (records: MutationRecord[]) => {
    if (records.length === 0) return
    batches += 1
    if (records.some((r) => (r.type === 'characterData' ? [r.target] : Array.from(r.addedNodes))
      .some((n) => n.textContent?.includes(text) === true))) added += 1
    if ((document.body.textContent ?? '').includes(text)) present += 1
  }
  const observer = new MutationObserver(scan)
  observer.observe(document.body, { subtree: true, childList: true, characterData: true })
  return () => { scan(observer.takeRecords()); observer.disconnect(); return { batches, added, present } }
}

const flush = () => act(async () => {})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  localStorage.clear()
  sessionStorage.clear()
  cleanup()
})

// Prompts (share no 8-character window with each other, with TYPED, or with 'Name of ').
const P1 = 'How does grappling work against a prone target underwater?'
const P2 = 'Sketch the lich vault beneath Karsk for tonight'
const TYPED = 'Qarth Obsidian Wyrmkeep'

describe('a cold load of /tavern (74j, T-4 to T-6)', () => {
  it('T-4 as a dm renders the tavern and reads the list exactly once, under StrictMode', async () => {
    const server = boot('/tavern', { strict: true })
    expect(await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeInTheDocument()
    await flush()
    expect(window.location.pathname).toBe('/tavern')
    expect(server.lines().filter((l) => l === 'GET /campaigns')).toHaveLength(1)
    expect(server.lines().some((l) => l.includes('/conversations'))).toBe(false)
    expect(server.calls.filter((c) => c.method === 'POST' || c.method === 'PATCH')).toEqual([])
  })

  it('T-5 as a player lands on Landing with no history entry and no campaign request; a dm is the control', async () => {
    const before = window.history.length
    const server = boot('/tavern', { role: 'player' })
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await flush()
    expect(window.location.pathname).toBe('/')
    expect(window.history.length).toBe(before)
    expect(server.scoped()).toEqual([])
    cleanup()
    const control = boot('/tavern', { role: 'dm' })
    await waitFor(() => expect(control.scoped()).toContain('GET /campaigns'))
  })

  it('T-6 a signed-out load shows Login, and after sign-in lands on Landing, never the tavern', async () => {
    const server = boot('/tavern', { signedOut: true })
    await screen.findByRole('button', { name: /sign in/i })
    expect(screen.queryByText('Your Campaigns')).toBeNull()
    vi.spyOn(api, 'login').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
    await userEvent.type(screen.getByLabelText('Email'), 'ada@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'pw')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await flush()
    expect(window.location.pathname).toBe('/')
    expect(screen.queryByText('Your Campaigns')).toBeNull()
    expect(server.calls.filter((c) => c.url.startsWith('/campaigns'))).toEqual([])
  })
})

describe('picking a campaign from the tavern (74j, T-8b, T-9, T-10, T-11, T-12a, T-14)', () => {
  /** Enter the Tavern -> GM chip -> Choose a campaign (the LeftNav entry). */
  async function openTavernFromGm(): Promise<void> {
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await userEvent.click(screen.getAllByRole('button', { name: 'GM' })[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Choose a campaign' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
  }

  it('T-9 lands in the campaign GM channel with no conversation open', async () => {
    boot('/')
    await openTavernFromGm()
    await userEvent.click(await screen.findByRole('button', { name: 'Name of cmp_A' }))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A'))
    expect(await screen.findByText('Campaign: Name of cmp_A')).toBeInTheDocument()
  })

  it("T-8b LeftNav's Choose a campaign opens the tavern from uncampaigned GM, and Back returns to it", async () => {
    boot('/')
    const before = window.history.length
    await openTavernFromGm()
    expect(window.location.pathname).toBe('/tavern')
    expect(window.history.length).toBe(before + 2) // workspace, then tavern
    act(() => { window.history.back() })
    await waitFor(() => expect(window.location.pathname).toBe('/workspace'))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'GM' })[0]).toHaveAttribute('aria-pressed', 'true'))
  })

  it('T-10 closes a legacy GM conversation on the pick; the first campaign send makes its own thread, naming neither the legacy id (Critic 44, kills M-16)', async () => {
    const server = bootProbed('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    await userEvent.click(await screen.findByRole('button', { name: 'New conversation' }))
    await userEvent.type(screen.getByPlaceholderText('Ask…'), `${P1}{Enter}`)
    // Controls: the legacy send is a POST /chat naming the legacy conversation, and it reached storage.
    await waitFor(() => expect(server.lines()).toContain('POST /chat'))
    const legacyChat = server.calls.find((c) => c.url === '/chat')
    const legacyId = /"conversation_id":"([^"]*)"/.exec(legacyChat?.body ?? '')?.[1] ?? null
    expect(legacyId).not.toBeNull()
    const L = legacyId as string
    expect(storageHolds(P1)).toBe(true)

    openTavern()
    const pick = server.calls.length
    await userEvent.click(await screen.findByRole('button', { name: 'Name of cmp_A' }))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A'))
    expect(live.raw.conversationId).toBeNull()

    const send = server.calls.length
    await userEvent.type(screen.getByPlaceholderText('Ask…'), `${P2}{Enter}`)
    await waitFor(() => expect(server.calls.slice(send).some((c) => c.url === '/chat')).toBe(true))

    expect(server.calls[send].method).toBe('POST')
    expect(server.calls[send].url).toBe('/conversations')
    expect(JSON.parse(server.calls[send].body ?? '{}')).toEqual({ schema_version: 1, started_mode: 'gm', campaign_id: 'cmp_A' })
    expect(server.calls[send + 1].url).toBe('/chat')
    expect(JSON.parse(server.calls[send + 1].body ?? '{}').conversation_id).toBe('cnv_new')
    // ModelPicker's own catalog read (GET /models) is unrelated to the
    // campaign/conversation surface this check is about (inferred decision:
    // the plan's whitelist did not name it, but it carries nothing scoped).
    // ChatPane reads the new thread's own timeline/attachments once it is the
    // active conversation (inferred decision: additive to the plan's
    // whitelist, both scoped to `cnv_new` -- never the legacy id).
    for (const call of server.calls.slice(pick)) {
      const isThreadCreate = call.method === 'POST' && call.url === '/conversations'
      const isChat = call.method === 'POST' && call.url === '/chat'
      const isThreadsList = call.method === 'GET' && call.url === '/conversations?campaign_id=cmp_A'
      const isModels = call.method === 'GET' && call.url === '/models'
      const isNewThreadDetail = call.method === 'GET' && call.url.startsWith('/conversations/cnv_new/')
      expect(isThreadCreate || isChat || isThreadsList || isModels || isNewThreadDetail).toBe(true)
    }
    expect(server.calls.slice(pick).some((c) => c.url.includes(L) || (c.body ?? '').includes(L))).toBe(false)
    expect(storageHolds(P2)).toBe(false)
  })

  it('T-11 first run: exactly one JSON create names the typed campaign, then the new campaign GM channel (Critic 48, kills M-14)', async () => {
    const server = bootProbed('/', { route: listOf([]) })
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Choose a campaign' }))
    await screen.findByText('Create your first campaign', { exact: false })
    await userEvent.type(screen.getByLabelText('Campaign name'), TYPED)
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_New'))

    const posts = server.calls.filter((c) => c.method === 'POST' && c.url === '/campaigns')
    expect(posts).toHaveLength(1)
    expect(posts[0].headers.get('Content-Type')).toBe('application/json')
    expect(JSON.parse(posts[0].body ?? '{}')).toEqual({ schema_version: 1, name: TYPED })
    expect(live.nav.mode).toBe('gm')
  })

  it('T-11 a 503 on create leaves the typed name and the campaign list reachable (Critic 48)', async () => {
    const route: Route = (c) => (c.method === 'POST' && c.url === '/campaigns' ? { status: 503 } : listOf([])(c))
    const server = bootProbed('/', { route })
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Choose a campaign' }))
    await screen.findByText('Create your first campaign', { exact: false })
    await userEvent.type(screen.getByLabelText('Campaign name'), TYPED)
    const postIndex = server.calls.length
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(server.calls.length).toBeGreaterThan(postIndex + 1))

    expect(window.location.pathname).toBe('/tavern')
    expect(screen.getByLabelText('Campaign name')).toHaveValue(TYPED)
    expect(server.calls.filter((c) => c.method === 'POST' && c.url === '/campaigns')).toHaveLength(1)
    const after = server.calls.slice(postIndex + 1)
    expect(after.every((c) => c.method === 'GET')).toBe(true)
    expect(after.some((c) => c.url === '/campaigns')).toBe(true)
  })

  it('T-12a Continue without a campaign clears the selection and lands in uncampaigned GM', async () => {
    boot('/workspace#campaign=cmp_A')
    await waitFor(() => expect(screen.getByText('Campaign: Name of cmp_A')).toBeInTheDocument())
    await userEvent.click(await screen.findByRole('button', { name: 'Switch campaign' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    await userEvent.click(screen.getByRole('button', { name: 'Continue without a campaign' }))
    await waitFor(() => expect(window.location.hash).toBe(''))
    expect(window.location.pathname).toBe('/workspace')
    expect(await screen.findByRole('button', { name: 'New conversation' })).toBeInTheDocument()
  })

  it('T-14 Back to chat returns to the GM workspace with the campaign unchanged', async () => {
    boot('/workspace#campaign=cmp_A')
    await waitFor(() => expect(screen.getByText('Campaign: Name of cmp_A')).toBeInTheDocument())
    await userEvent.click(await screen.findByRole('button', { name: 'Switch campaign' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    await userEvent.click(await screen.findByRole('button', { name: 'Back to chat' }))
    await waitFor(() => expect(window.location.pathname + window.location.hash).toBe('/workspace#campaign=cmp_A'))
    expect(screen.getByText('Campaign: Name of cmp_A')).toBeInTheDocument()
  })
})
