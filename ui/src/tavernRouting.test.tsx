/**
 * tavernRouting.test.tsx -- the /tavern screen in the app that ships
 * (agent-forge-harness-74j, brief section 7, T-4 to T-14).
 *
 * Mounts `AppRoot` -- the component `main.tsx` renders, as
 * `campaignRouting.test.tsx` does -- with `fetch` replaced by a recorder, so
 * every claim about which request is made, and which never is, is about the
 * real provider tower.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppRoot } from './AppRoot'
import * as api from './api'

interface Call { url: string; method: string; body: string | null; reply: (status: number, body?: unknown) => void }

function campaignBody(id: string) {
  return {
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
  }
}
const page = (items: unknown[]) => ({ schema_version: 1, items, next_cursor: null })

interface BootOptions { role?: 'dm' | 'player'; signedOut?: boolean; defer?: string; strict?: boolean; list?: unknown[] }

/** Stub `fetch` and `getMe`, put `url` in the address bar and mount AppRoot. */
function boot(url: string, options: BootOptions = {}) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null,
      reply: (status, body) => resolve(new Response(JSON.stringify(body ?? {}), { status })),
    }
    calls.push(call)
    if (call.url === options.defer) return
    if (call.url === '/campaigns' && call.method === 'GET') {
      call.reply(200, page(options.list ?? [campaignBody('cmp_A')]))
      return
    }
    const one = /^\/campaigns\/(cmp_\w+)$/.exec(call.url)
    if (one !== null) { call.reply(200, campaignBody(one[1])); return }
    if (call.method === 'POST' && call.url === '/campaigns') {
      call.reply(201, campaignBody('cmp_New'))
      return
    }
    if (call.method === 'POST' && call.url === '/conversations') {
      const campaignId = /"campaign_id":"(\w+)"/.exec(call.body ?? '')?.[1] ?? 'cmp_A'
      call.reply(201, {
        schema_version: 1, conversation_id: 'cnv_new', campaign_id: campaignId, title: null,
        started_mode: 'gm', created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
      })
      return
    }
    if (/^\/conversations\?campaign_id=/.test(call.url)) { call.reply(200, page([])); return }
    call.reply(call.url === '/chat' ? 200 : 404, call.url === '/chat'
      ? { answer: 'ok', sources: [], answerable: true, conversation_id: bodyId(call.body) }
      : {})
  }))
  vi.spyOn(api, 'getMe').mockResolvedValue(options.signedOut === true
    ? { kind: 'error', status: 401, message: 'not signed in' }
    : { kind: 'ok', user: { email: 'ada@example.com', role: options.role ?? 'dm' } })
  window.history.replaceState(null, '', url)
  render(options.strict === true ? <React.StrictMode><AppRoot /></React.StrictMode> : <AppRoot />)
  const lines = () => calls.map((c) => `${c.method} ${c.url}`)
  const scoped = () => lines().filter((line) => / \/(campaigns|conversations)/.test(line))
  return { calls, lines, scoped }
}

function bodyId(body: string | null): string {
  return /"conversation_id":"(\w+)"/.exec(body ?? '')?.[1] ?? /"campaign_id":"(\w+)"/.exec(body ?? '')?.[1] ?? 'cnv_1'
}

const flush = () => act(async () => {})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  cleanup()
})

describe('a cold load of /tavern (74j, T-4 to T-6)', () => {
  it('as a dm renders the tavern and reads the list exactly once, under StrictMode', async () => {
    const server = boot('/tavern', { strict: true })
    expect(await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeInTheDocument()
    await flush()
    expect(window.location.pathname).toBe('/tavern')
    expect(server.lines().filter((l) => l === 'GET /campaigns')).toHaveLength(1)
    expect(server.lines().some((l) => l.includes('/conversations'))).toBe(false)
    expect(server.calls.filter((c) => c.method === 'POST' || c.method === 'PATCH')).toEqual([])
  })

  it('as a player lands on Landing with no history entry and no campaign request; a dm is the control', async () => {
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

  it('a signed-out load shows Login, and after sign-in lands on Landing, never the tavern', async () => {
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

describe('picking a campaign from the tavern (74j, T-9, T-10, T-11, T-12a, T-14)', () => {
  /** Enter the Tavern -> GM chip -> Choose a campaign (the LeftNav entry). */
  async function openTavernFromGm(): Promise<void> {
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await userEvent.click(screen.getAllByRole('button', { name: 'GM' })[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Choose a campaign' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
  }

  it('lands in the campaign GM channel with no conversation open', async () => {
    boot('/')
    await openTavernFromGm()
    await userEvent.click(await screen.findByRole('button', { name: 'Name of cmp_A' }))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A'))
    expect(await screen.findByText('Campaign: Name of cmp_A')).toBeInTheDocument()
  })

  it("LeftNav's Choose a campaign opens the tavern from uncampaigned GM, and Back returns to it (T-8b)", async () => {
    boot('/')
    const before = window.history.length
    await openTavernFromGm()
    expect(window.location.pathname).toBe('/tavern')
    expect(window.history.length).toBe(before + 2) // workspace, then tavern
    act(() => { window.history.back() })
    await waitFor(() => expect(window.location.pathname).toBe('/workspace'))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'GM' })[0]).toHaveAttribute('aria-pressed', 'true'))
  })

  it('closes a legacy GM conversation on the pick; the first campaign send makes its own thread, naming neither the legacy id (RAIL-26)', async () => {
    const server = boot('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await userEvent.click(screen.getAllByRole('button', { name: 'GM' })[0])
    await userEvent.click(screen.getAllByRole('button', { name: 'New conversation' })[0])
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'A legacy question about the old heist plan{Enter}')
    // Control: the legacy send is a POST /chat naming the legacy conversation.
    await waitFor(() => expect(server.lines()).toContain('POST /chat'))
    const legacyChat = server.calls.find((c) => c.url === '/chat')
    const legacyId = /"conversation_id":"([^"]*)"/.exec(legacyChat?.body ?? '')?.[1] ?? null
    expect(legacyId).not.toBeNull()

    await userEvent.click(await screen.findByRole('button', { name: 'Choose a campaign' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    const before = server.calls.length
    await userEvent.click((await screen.findAllByRole('button', { name: 'Name of cmp_A' }))[0])
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A'))

    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'A brand new campaign question about the crypt{Enter}')
    await waitFor(() => expect(server.lines().slice(before)).toContain('POST /chat'))
    const after = server.calls.slice(before)
    expect(after.some((c) => c.url === '/conversations' && c.method === 'POST')).toBe(true)
    expect(after.some((c) => c.body?.includes(legacyId as string) === true)).toBe(false)
  })

  it('first run: the empty state, one JSON create, then the new campaign GM channel', async () => {
    boot('/', { list: [] })
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the Tavern' }))
    await userEvent.click(screen.getAllByRole('button', { name: 'GM' })[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Choose a campaign' }))
    expect(await screen.findByText('Create your first campaign', { exact: false })).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Campaign name'), 'The Sunken Library')
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_New'))
    expect(await screen.findByText('Campaign: Name of cmp_New')).toBeInTheDocument()
  })

  it('Continue without a campaign clears the selection and lands in uncampaigned GM', async () => {
    boot('/workspace#campaign=cmp_A')
    await waitFor(() => expect(screen.getByText('Campaign: Name of cmp_A')).toBeInTheDocument())
    await userEvent.click(await screen.findByRole('button', { name: 'Switch campaign' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    await userEvent.click(screen.getByRole('button', { name: 'Continue without a campaign' }))
    await waitFor(() => expect(window.location.hash).toBe(''))
    expect(window.location.pathname).toBe('/workspace')
    expect(await screen.findByRole('button', { name: 'New conversation' })).toBeInTheDocument()
  })

  it('Back to chat returns to the GM workspace with the campaign unchanged', async () => {
    boot('/workspace#campaign=cmp_A')
    await waitFor(() => expect(screen.getByText('Campaign: Name of cmp_A')).toBeInTheDocument())
    await userEvent.click(await screen.findByRole('button', { name: 'Switch campaign' }))
    await screen.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    await userEvent.click(screen.getByRole('button', { name: 'Back to chat' }))
    await waitFor(() => expect(window.location.pathname + window.location.hash).toBe('/workspace#campaign=cmp_A'))
    expect(screen.getByText('Campaign: Name of cmp_A')).toBeInTheDocument()
  })
})
