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
 * batches after which `text` is on screen.
 *
 * `addedNodes` holds LIVE references, and a cascading effect (`loadCampaigns()`
 * runs the instant its component mounts) can insert a WHOLE subtree and then,
 * still inside the same batch, remove one of that subtree's own descendants --
 * reading the ancestor's `textContent` back only ever sees the settled result,
 * never the removed child's text. A `removedNodes` match is therefore read
 * instead: the detached node itself is never mutated again, so its text is
 * frozen at whatever it was when it left. The one case that is NOT a flash is
 * text that was already on screen before this watcher started (T-13a: the row
 * is visible, then an account switch removes it) -- `alreadyThere` excludes
 * exactly that, by baselining presence once, at the moment watching begins. */
function watch(text: string): () => { batches: number; added: number; present: number } {
  let batches = 0; let added = 0; let present = 0
  const holds = (n: Node | null): boolean => n?.textContent?.includes(text) === true
  const alreadyThere = (document.body.textContent ?? '').includes(text)
  const scan = (records: MutationRecord[]) => {
    if (records.length === 0) return
    batches += 1
    let flashed = false
    for (const r of records) {
      if (r.type === 'characterData') {
        if (holds(r.target) || r.oldValue?.includes(text) === true) flashed = true
        continue
      }
      if (Array.from(r.addedNodes).some(holds)) flashed = true
      if (!alreadyThere && Array.from(r.removedNodes).some(holds)) flashed = true
    }
    if (flashed) added += 1
    if ((document.body.textContent ?? '').includes(text)) present += 1
  }
  const observer = new MutationObserver(scan)
  observer.observe(document.body, { subtree: true, childList: true, characterData: true, characterDataOldValue: true })
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

  it('T-6 a signed-out load shows Login, and after sign-in lands on Landing, never the tavern; openTavern is the control (kills M-9)', async () => {
    const server = bootProbed('/tavern', { signedOut: true })
    await screen.findByRole('button', { name: /sign in/i })
    const w = watch('Your Campaigns')
    vi.spyOn(api, 'login').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
    await userEvent.type(screen.getByLabelText('Email'), 'ada@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'pw')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await flush()
    const signedIn = w()
    expect(signedIn.batches).toBeGreaterThanOrEqual(1)
    expect(signedIn.added).toBe(0)
    expect(signedIn.present).toBe(0)
    expect(window.location.pathname).toBe('/')
    expect(server.calls.filter((c) => c.url.startsWith('/campaigns'))).toEqual([])
    // Control, same harness: the tavern does read the list here, and watch() does see its heading.
    const control = watch('Your Campaigns')
    openTavern()
    await waitFor(() => expect(server.lines()).toContain('GET /campaigns'))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(control().added).toBeGreaterThanOrEqual(1)
  })
})

describe('picking a campaign from the tavern (74j, T-9, T-10, T-11, T-12a, T-14)', () => {
  it('T-9 a pick lands selected in its GM channel with no conversation open; Back gives /tavern with an empty hash (kills M-14, X-frag)', async () => {
    bootProbed('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Prep Name of cmp_A' }))
    await waitFor(() => expect(window.location.pathname + window.location.hash).toBe('/workspace#campaign=cmp_A'))
    expect(live.c.selection.kind).toBe('selected')
    expect(live.nav.mode).toBe('gm')
    expect(live.raw.conversationId).toBeNull()
    expect(await screen.findByText('Campaign: Name of cmp_A')).toBeInTheDocument()
    // I-7's write half (T-7 is the read half): the /tavern entry carries no key while a campaign is selected.
    act(() => { window.history.back() })
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    await flush()
    await flush()
    expect(window.location.pathname).toBe('/tavern')
    expect(window.location.hash).toBe('')
  })

  it('T-10 closes a legacy GM conversation on the pick; the first campaign send makes its own thread, naming neither the legacy id (Critic 44, kills M-16)', async () => {
    const server = bootProbed('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
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
    await userEvent.click(await screen.findByRole('button', { name: 'Prep Name of cmp_A' }))
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
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
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
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
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

describe('a hostile fragment and web-storage leaks (74j, H-1, H-2)', () => {
  it('T-7 a hostile fragment on /tavern selects nothing, names neither id and is stripped to foreign keys (kills M-10)', async () => {
    const server = boot('/tavern#campaign=cmp_A&conversation=cnv_1&x=1')
    const item = await screen.findByRole('button', { name: 'Prep Name of cmp_A' })
    await flush()
    await flush()
    expect(server.calls.length).toBeGreaterThan(0)
    expect(server.calls.every((c) => c.method === 'GET')).toBe(true)
    expect(server.calls.some((c) => c.url === '/campaigns')).toBe(true)
    expect(server.calls.some((c) => c.url.includes('cmp_A') || c.url.includes('cnv_1')
      || (c.body ?? '').includes('cmp_A') || (c.body ?? '').includes('cnv_1'))).toBe(false)
    expect(window.location.hash).toBe('#x=1')
    // Two added assertions (inferred decision): the same claim -- "selects
    // nothing" -- read straight from the DOM, not only from the request log.
    // (30c: a campaign card has no pressed state, so the card is shown and
    // Continue without a campaign -- offered only once something is selected --
    // is absent.)
    expect(item).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Continue without a campaign' })).toBeNull()
  })

  it('T-15 no campaign id, name or typed name reaches web storage, and the page title never changes (Critic 45, kills M-23 and M-44)', async () => {
    const title = document.title
    const secrets = ['cmp_tavern_probe_1', 'Name of cmp_tavern_probe_1', 'cmp_A', 'Name of cmp_A', 'cmp_New', TYPED]
    const check = (step: string): void => {
      for (const s of secrets) expect(storageHolds(s), `${step}: ${s}`).toBe(false)
      expect(document.title, step).toBe(title)
    }

    const server = bootProbed('/tavern', { route: listOf([campaignBody('cmp_tavern_probe_1'), campaignBody('cmp_A')]) })

    // 1. Short-text control (Critic 45): storageHolds itself can see a planted value.
    sessionStorage.setItem('probe', 'cmp_A')
    expect(storageHolds('cmp_A')).toBe(true)
    sessionStorage.removeItem('probe')

    // 2. Cold load.
    await screen.findByRole('button', { name: 'Prep Name of cmp_tavern_probe_1' })
    check('cold load')

    // 3. Legacy control: a plain prompt still reaches the recall store.
    await userEvent.click(await screen.findByRole('button', { name: 'Back to chat' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    await userEvent.click(await screen.findByRole('button', { name: 'New conversation' }))
    await userEvent.type(screen.getByPlaceholderText('Ask…'), `${P1}{Enter}`)
    await waitFor(() => expect(server.lines()).toContain('POST /chat'))
    expect(storageHolds(P1)).toBe(true)

    // 4. The pick.
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Prep Name of cmp_tavern_probe_1' }))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_tavern_probe_1'))
    check('pick')

    // 5. The create (the tavern now renders with a selected campaign).
    openTavern()
    await userEvent.type(await screen.findByLabelText('Campaign name'), TYPED)
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_New'))
    check('create')

    // 6. Continue.
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Continue without a campaign' }))
    await waitFor(() => expect(window.location.hash).toBe(''))
    expect(window.location.pathname).toBe('/workspace')
    check('continue')

    // 7. Sign-out.
    vi.spyOn(api, 'logout').mockResolvedValue(true)
    await act(async () => { await live.user.user.signOut() })
    check('sign-out')
  })
})

describe('tavern history, picks, vetoes and Continue (74j, H-3)', () => {
  /** Enter the workspace (the Sage chip), GM, open `open`: the heading renders as /tavern's `h1`,
   * focused, with a fresh history entry and no own fragment key; Back returns
   * to the workspace unchanged, and Forward reopens the tavern. */
  async function expectTavernRoundTrip(open: () => Promise<void> | void, focus: 'prep' | 'heading' = 'prep'): Promise<void> {
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    const kind = live.c.selection.kind
    const len = window.history.length
    await open()
    const heading = await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(window.location.pathname).toBe('/tavern')
    expect(window.history.length).toBe(len + 1)
    if (focus === 'prep') {
      // 30c ID-4: once the list settles, focus moves on to the top card's Prep.
      await waitFor(() => expect(screen.getByRole('button', { name: 'Prep Name of cmp_A' })).toHaveFocus())
    } else {
      // An empty list has no card to move to: focus stays on the heading.
      await screen.findByText('Create your first campaign', { exact: false })
      await flush()
      expect(document.activeElement).toBe(heading)
    }
    expect(window.location.hash).not.toMatch(/campaign=/)
    expect(window.location.hash).not.toMatch(/conversation=/)
    act(() => { window.history.back() })
    await waitFor(() => expect(window.location.pathname).toBe('/workspace'))
    expect(live.nav.mode).toBe('gm')
    expect(live.c.selection.kind).toBe(kind)
    act(() => { window.history.forward() })
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(window.location.pathname).toBe('/tavern')
  }

  it('T-8 openTavern opens the tavern from uncampaigned GM and round-trips through history, focused (kills M-11, M-12, M-13)', async () => {
    bootProbed('/')
    await expectTavernRoundTrip(() => { openTavern() })
  })

  it('T-30 the Landing CTA opens /tavern for a dm: one new history entry, and Back gives / (30c ID-1)', async () => {
    const server = bootProbed('/')
    const len = window.history.length
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    expect(window.location.pathname).toBe('/tavern')
    expect(window.history.length).toBe(len + 1)
    await waitFor(() => expect(server.lines()).toContain('GET /campaigns'))
    act(() => { window.history.back() })
    await waitFor(() => expect(window.location.pathname).toBe('/'))
    expect(await screen.findByRole('button', { name: 'Enter the Tavern' })).toBeInTheDocument()
  })

  it('T-30 control: the CTA of a player still opens the workspace and makes no campaign request', async () => {
    const server = bootProbed('/', { role: 'player' })
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await waitFor(() => expect(window.location.pathname).toBe('/workspace'))
    expect(screen.queryByRole('heading', { name: 'Your Campaigns' })).toBeNull()
    await flush()
    expect(server.scoped()).toEqual([])
  })

  it('T-8c with no campaigns openTavern still lands focus on the heading and round-trips through history (30c ID-4)', async () => {
    bootProbed('/', { route: listOf([]) })
    await expectTavernRoundTrip(() => { openTavern() }, 'heading')
  })

  it("T-8b LeftNav's Choose a campaign opens the tavern from uncampaigned GM and round-trips through history, focused (kills M-48)", async () => {
    bootProbed('/')
    await expectTavernRoundTrip(async () => {
      await userEvent.click(await screen.findByRole('button', { name: 'Choose a campaign' }))
    })
  })

  it('T-9 veto a guard that refuses the pick leaves the tavern open and the selection at none (kills M-15)', async () => {
    bootProbed('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    openTavern()
    await screen.findByRole('button', { name: 'Prep Name of cmp_A' })
    const before = live.raw.conversationId
    live.c.registerSwitchGuard(() => false)
    await userEvent.click(screen.getByRole('button', { name: 'Prep Name of cmp_A' }))
    await flush()
    expect(window.location.pathname).toBe('/tavern')
    expect(live.c.selection.kind).toBe('none')
    expect(live.raw.conversationId).toBe(before)
  })

  it('T-9b picking the already-selected campaign is unchanged: no guard runs and no request is made, but the open conversation still clears (kills M-35 and M-16)', async () => {
    const server = bootProbed('/workspace#campaign=cmp_A&conversation=cnv_T', { route: listOf([campaignBody('cmp_A'), campaignBody('cmp_B')]) })
    await waitFor(() => expect(live.raw?.conversationId).toBe('cnv_T'))
    const seen: Array<{ campaignId: string | null }> = []
    live.c.registerSwitchGuard((next) => { seen.push(next); return true })
    openTavern()
    await screen.findByRole('button', { name: 'Prep Name of cmp_A' })
    const len = window.history.length
    const mark = server.calls.length
    await userEvent.click(screen.getByRole('button', { name: 'Prep Name of cmp_A' }))
    await waitFor(() => expect(window.location.pathname + window.location.hash).toBe('/workspace#campaign=cmp_A'))
    expect(live.nav.mode).toBe('gm')
    expect(live.raw.conversationId).toBeNull()
    expect(window.history.length).toBe(len + 1)
    expect(seen).toEqual([])
    expect(server.calls.slice(mark).some((c) => c.url.startsWith('/campaigns'))).toBe(false)
    expect(server.lines()).toContain('GET /campaigns/cmp_A')

    // Guard control: a real switch does run the guard.
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Prep Name of cmp_B' }))
    await waitFor(() => expect(seen).toEqual([{ campaignId: 'cmp_B' }]))
  })

  it.each([503, 404])('T-12b Continue clears a %i-failed restore and lands in uncampaigned GM (kills M-20)', async (status) => {
    const route: Route = (c) => (c.method === 'GET' && c.url === '/campaigns/cmp_A' ? { status } : defaultRoute(c))
    bootProbed('/workspace#campaign=cmp_A', { route })
    await waitFor(() => expect(['failed', 'unavailable']).toContain(live.c.selection.kind))
    openTavern()
    const continueButton = await screen.findByRole('button', { name: 'Continue without a campaign' })
    await userEvent.click(continueButton)
    await waitFor(() => expect(live.c.selection.kind).toBe('none'))
    expect(window.location.pathname).toBe('/workspace')
    expect(live.nav.mode).toBe('gm')
    cleanup()
    vi.unstubAllGlobals()
    window.history.replaceState(null, '', '/')
  })

  it('T-12b the none case: no Continue button when nothing was ever selected (positive control)', async () => {
    bootProbed('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    openTavern()
    await screen.findByRole('button', { name: 'Prep Name of cmp_A' })
    expect(screen.queryByRole('button', { name: 'Continue without a campaign' })).toBeNull()
  })

  it('T-12c a veto leaves the tavern open with the campaign still selected (kills M-19)', async () => {
    bootProbed('/workspace#campaign=cmp_A&conversation=cnv_T')
    await waitFor(() => expect(live.raw.conversationId).toBe('cnv_T'))
    live.c.registerSwitchGuard(() => false)
    openTavern()
    await userEvent.click(await screen.findByRole('button', { name: 'Continue without a campaign' }))
    await flush()
    expect(window.location.pathname).toBe('/tavern')
    expect(live.c.selection.kind).toBe('selected')
    expect(live.raw.conversationId).toBe('cnv_T')
  })

  it('T-12c pressing Continue twice while guards are pending still resolves to one switch (kills M-19)', async () => {
    bootProbed('/workspace#campaign=cmp_A&conversation=cnv_T')
    await waitFor(() => expect(live.raw.conversationId).toBe('cnv_T'))
    const resolvers: Array<(value: boolean) => void> = []
    live.c.registerSwitchGuard(() => new Promise<boolean>((resolve) => { resolvers.push(resolve) }))
    openTavern()
    const len = window.history.length
    const button = await screen.findByRole('button', { name: 'Continue without a campaign' })
    await userEvent.click(button)
    await userEvent.click(button)
    await waitFor(() => expect(resolvers.length).toBeGreaterThan(0))
    act(() => { for (const resolve of resolvers) resolve(true) })
    await waitFor(() => expect(window.location.pathname).toBe('/workspace'))
    expect(live.c.selection.kind).toBe('none')
    expect(window.history.length).toBe(len + 1)
  })

  it('T-12d a Continue pressed while a create is pending is vetoed; the create still lands (kills M-37)', async () => {
    let held: Call | null = null
    const route: Route = (c) => {
      if (c.method === 'POST' && c.url === '/campaigns') { held = c; return 'defer' }
      return defaultRoute(c)
    }
    bootProbed('/workspace#campaign=cmp_A', { route })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    openTavern()
    const len = window.history.length
    await userEvent.type(await screen.findByLabelText('Campaign name'), TYPED)
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(held).not.toBeNull())
    await userEvent.click(screen.getByRole('button', { name: 'Continue without a campaign' }))
    await flush()
    expect(window.location.pathname).toBe('/tavern')
    expect(live.c.selection.kind).toBe('selected')
    if (live.c.selection.kind === 'selected') expect(live.c.selection.campaign.campaign_id).toBe('cmp_A')
    act(() => { held?.reply({ status: 201, body: { ...campaignBody('cmp_New'), name: TYPED } }) })
    await waitFor(() => expect(window.location.pathname + window.location.hash).toBe('/workspace#campaign=cmp_New'))
    expect(window.history.length).toBe(len + 1)
  })

  it('T-27 a restore that resolves while the tavern is open does not add a history entry (kills M-31)', async () => {
    let held: Call | null = null
    const route: Route = (c) => {
      if (c.method === 'GET' && c.url === '/campaigns/cmp_A') { held = c; return 'defer' }
      return defaultRoute(c)
    }
    bootProbed('/workspace#campaign=cmp_A', { route })
    await waitFor(() => expect(live.c.selection.kind).toBe('restoring'))
    openTavern()
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    const len = window.history.length
    act(() => { held?.reply({ status: 200, body: campaignBody('cmp_A') }) })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await flush()
    expect(window.location.pathname).toBe('/tavern')
    expect(window.history.length).toBe(len)
  })

  it('T-28 leaving mid-create to the legacy conversation does not strand it; the create still lands on the next GM visit (kills M-32)', async () => {
    let held: Call | null = null
    const route: Route = (c) => {
      if (c.method === 'POST' && c.url === '/campaigns') { held = c; return 'defer' }
      return defaultRoute(c)
    }
    const server = bootProbed('/', { route })
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    await userEvent.click(await screen.findByRole('button', { name: 'New conversation' }))
    await userEvent.type(screen.getByPlaceholderText('Ask…'), `${P1}{Enter}`)
    await waitFor(() => expect(server.lines()).toContain('POST /chat'))
    const legacyChat = server.calls.find((c) => c.url === '/chat')
    const L = /"conversation_id":"([^"]*)"/.exec(legacyChat?.body ?? '')?.[1] ?? null
    expect(L).not.toBeNull()

    openTavern()
    await userEvent.type(await screen.findByLabelText('Campaign name'), TYPED)
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(held).not.toBeNull())

    await userEvent.click(await screen.findByRole('button', { name: 'Back to chat' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'Sage' }))
    const len = window.history.length

    act(() => { held?.reply({ status: 201, body: { ...campaignBody('cmp_New'), name: TYPED } }) })
    await flush()
    expect(live.nav.mode).toBe('sage')
    expect(window.location.pathname).toBe('/workspace')
    expect(window.history.length).toBe(len)
    expect(live.raw.conversationId).toBe(L)

    const rel = server.calls.length
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    const send = server.calls.length
    await userEvent.type(screen.getByPlaceholderText('Ask…'), `${P2}{Enter}`)
    await waitFor(() => expect(server.calls.slice(send).some((c) => c.url === '/chat')).toBe(true))

    expect(server.calls[send].method).toBe('POST')
    expect(server.calls[send].url).toBe('/conversations')
    expect(JSON.parse(server.calls[send].body ?? '{}').campaign_id).toBe('cmp_New')
    expect(server.calls[send + 1].url).toBe('/chat')
    expect(JSON.parse(server.calls[send + 1].body ?? '{}').conversation_id).toBe('cnv_new')
    // Inferred decision, as in T-10: ModelPicker's catalog read and the new
    // thread's own timeline/attachments are unrelated to the leak this checks.
    for (const call of server.calls.slice(rel)) {
      const isThreadCreate = call.method === 'POST' && call.url === '/conversations'
      const isChat = call.method === 'POST' && call.url === '/chat'
      const isThreadsList = call.method === 'GET' && call.url === '/conversations?campaign_id=cmp_New'
      const isModels = call.method === 'GET' && call.url === '/models'
      const isNewThreadDetail = call.method === 'GET' && call.url.startsWith('/conversations/cnv_new/')
      expect(isThreadCreate || isChat || isThreadsList || isModels || isNewThreadDetail).toBe(true)
    }
    expect(server.calls.slice(rel).some((c) => c.url.includes(L as string) || (c.body ?? '').includes(L as string))).toBe(false)
  })
})

describe('tavern visits and account changes (74j, H-3)', () => {
  it('T-4b revisiting /tavern never flashes the empty state while the list reloads (kills M-34)', async () => {
    let getCount = 0
    let held: Call | null = null
    const route: Route = (c) => {
      if (c.method === 'GET' && c.url === '/campaigns') {
        getCount += 1
        if (getCount === 2) { held = c; return 'defer' }
      }
      return defaultRoute(c)
    }
    bootProbed('/tavern', { route })
    await screen.findByRole('button', { name: 'Prep Name of cmp_A' })
    await userEvent.click(await screen.findByRole('button', { name: 'Back to chat' }))
    const n = getCount

    const a = watch('Name of cmp_A')
    const e = watch('Create your first campaign')
    openTavern()
    await waitFor(() => expect(getCount).toBe(n + 1))

    act(() => { held?.reply({ status: 200, body: page([campaignBody('cmp_A'), campaignBody('cmp_B')]) }) })
    await screen.findByRole('button', { name: 'Prep Name of cmp_B' })
    const aResult = a()
    const eResult = e()
    expect(aResult.batches).toBeGreaterThanOrEqual(1)
    expect(aResult.present).toBe(aResult.batches)
    expect(eResult.added).toBe(0)
    expect(eResult.present).toBe(0)
  })

  it('T-4c a 401 on the campaigns read drops to Login without a loop; signing back in makes no second read (kills M-43)', async () => {
    const route: Route = (c) => (c.method === 'GET' && c.url === '/campaigns' ? { status: 401 } : defaultRoute(c))
    const server = boot('/tavern', { route })
    await screen.findByRole('button', { name: /sign in/i })
    await flush()
    expect(server.calls.filter((c) => c.url === '/campaigns')).toHaveLength(1)
    vi.spyOn(api, 'login').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
    await userEvent.type(screen.getByLabelText('Email'), 'ada@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'pw')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await flush()
    expect(window.location.pathname).toBe('/')
    expect(server.calls.filter((c) => c.url === '/campaigns')).toHaveLength(1)
  })

  it("T-5b signing in as a player over a parked tavern history entry never re-requests it (kills M-7)", async () => {
    const server = bootProbed('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
    openTavern()
    await waitFor(() => expect(server.lines()).toContain('GET /campaigns'))
    await userEvent.click(await screen.findByRole('button', { name: 'Back to chat' }))
    const L1 = window.history.length
    const mark = server.calls.length

    act(() => { live.user.signIn({ email: 'bob@example.com', role: 'player' }) })
    await screen.findByRole('button', { name: 'Enter the Tavern' })
    const L2 = window.history.length

    const popped: string[] = []
    const onPopState = (): void => { popped.push(window.location.pathname) }
    window.addEventListener('popstate', onPopState)
    act(() => { window.history.go(L2 === L1 + 1 ? -2 : -1) })
    await waitFor(() => expect(popped).toContain('/tavern'))
    await waitFor(() => expect(window.location.pathname).toBe('/'))
    window.removeEventListener('popstate', onPopState)

    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    expect(window.history.length).toBe(L2)
    expect(server.calls.slice(mark).some((c) => c.url.startsWith('/campaigns'))).toBe(false)
  })

  it("T-13a an account switch never shows the previous account's campaign, even for one frame (kills M-21)", async () => {
    let owner = 'ada'
    const route: Route = (c) => (c.method === 'GET' && c.url === '/campaigns'
      ? { status: 200, body: page([campaignBody(owner === 'ada' ? 'cmp_A' : 'cmp_B')]) }
      : defaultRoute(c))
    const server = bootProbed('/tavern', { route })
    await screen.findByRole('button', { name: 'Prep Name of cmp_A' })

    const mark = server.calls.length
    const w = watch('Name of cmp_A')
    owner = 'bob'
    act(() => { live.user.signIn({ email: 'bob@example.com', role: 'dm' }) })
    await screen.findByText('Enter the Tavern')

    const result = w()
    expect(result.batches).toBeGreaterThanOrEqual(1)
    expect(result.present).toBe(0)
    expect(result.added).toBe(0)
    expect(server.calls.slice(mark).some((c) => c.url.includes('cmp_A') || (c.body ?? '').includes('cmp_A'))).toBe(false)
  })

  it('T-13b a role change at /tavern (same account) resets mode and leaves without a history entry (kills M-41 and M-42)', async () => {
    const server = bootProbed('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Sage' }))
    await userEvent.click(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'GM' }))
    openTavern()
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    const len = window.history.length
    const mark = server.calls.length

    act(() => { live.user.signIn({ email: 'ada@example.com', role: 'player' }) })
    await screen.findByText('Enter the Tavern')

    expect(window.location.pathname).toBe('/')
    expect(live.nav.mode).toBe('sage')
    expect(window.history.length).toBe(len)
    expect(server.calls.slice(mark).some((c) => c.url.startsWith('/campaigns'))).toBe(false)
  })

  it('T-29 a cold /tavern load never flashes the empty state while the first read is in flight (kills M-40)', async () => {
    let held: Call | null = null
    const route: Route = (c) => {
      if (c.method === 'GET' && c.url === '/campaigns') { held = c; return 'defer' }
      return defaultRoute(c)
    }
    const w = watch('Create your first campaign')
    boot('/tavern', { route })
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    act(() => { held?.reply({ status: 200, body: page([campaignBody('cmp_A')]) }) })
    await screen.findByRole('button', { name: 'Prep Name of cmp_A' })
    const result = w()
    expect(result.batches).toBeGreaterThanOrEqual(1)
    expect(result.added).toBe(0)
    expect(result.present).toBe(0)
  })
})
