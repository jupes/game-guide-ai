/**
 * campaignRouting.test.tsx -- the campaign restore in the app that ships
 * (agent-forge-harness-1kg.2.5, PR-1b; brief section 7.4 and the Critic's
 * items 3, 6, 7, 19 and 22).
 *
 * Mounts `AppRoot` -- the component `main.tsx` renders, as
 * `appRouting.test.tsx` does -- with `fetch` replaced by a recorder that can
 * hold an answer back, so every claim about which request comes first, and
 * which never comes, is about the real provider tower. Each "no request"
 * assertion has its positive control in the same test.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppRoot } from './AppRoot'
import * as api from './api'

interface Call { url: string; method: string; reply: (status: number, body?: unknown) => void }

function campaignBody(id: string) {
  return {
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
  }
}
const THREAD = {
  schema_version: 1, conversation_id: 'cnv_1', campaign_id: 'cmp_A', title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
}

interface BootOptions { role?: 'dm' | 'player'; signedOut?: boolean; defer?: string; strict?: boolean }

/** Stub `fetch` and `getMe`, put `url` in the address bar and mount AppRoot. */
function boot(url: string, options: BootOptions = {}) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET',
      reply: (status, body) => resolve(new Response(JSON.stringify(body ?? {}), { status })),
    }
    calls.push(call)
    if (call.url === options.defer) return
    const one = /^\/campaigns\/(cmp_\w+)$/.exec(call.url)
    if (one !== null) call.reply(200, campaignBody(one[1]))
    else if (call.url === '/conversations/cnv_1') call.reply(200, THREAD)
    // PR-2: a campaign's first GM send creates its thread before /chat.
    else if (call.method === 'POST' && call.url === '/conversations') call.reply(201, { ...THREAD, conversation_id: 'cnv_new', campaign_id: 'cmp_B' })
    else call.reply(call.url === '/chat' ? 503 : 404)
  }))
  vi.spyOn(api, 'getMe').mockResolvedValue(options.signedOut === true
    ? { kind: 'error', status: 401, message: 'not signed in' }
    : { kind: 'ok', user: { email: 'ada@example.com', role: options.role ?? 'dm' } })
  window.history.replaceState(null, '', url)
  render(options.strict === true ? <React.StrictMode><AppRoot /></React.StrictMode> : <AppRoot />)
  const lines = () => calls.map((c) => `${c.method} ${c.url}`)
  /** Requests for anything campaign-scoped: campaigns, and every conversation route. */
  const scoped = () => lines().filter((line) => / \/(campaigns|conversations)/.test(line))
  return { calls, lines, scoped }
}

/** The positive control: the same recorder, a dm, a campaign fragment. */
async function restoresAfterwards(server: ReturnType<typeof boot>): Promise<void> {
  cleanup()
  vi.mocked(api.getMe).mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  window.history.replaceState(null, '', '/workspace#campaign=cmp_A')
  render(<AppRoot />)
  await waitFor(() => expect(server.scoped()).toContain('GET /campaigns/cmp_A'))
}

const flush = () => act(async () => {})
const TIMELINE = 'GET /conversations/cnv_1/timeline'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('a campaign deep link', () => {
  it('asks the server about the campaign first and about the thread only after it answers (T1-1, T1-2)', async () => {
    const server = boot('/workspace#campaign=cmp_A&conversation=cnv_1', { defer: '/campaigns/cmp_A' })
    await waitFor(() => expect(server.scoped()).toEqual(['GET /campaigns/cmp_A']))
    await flush()
    expect(server.scoped()).toEqual(['GET /campaigns/cmp_A'])
    expect(window.location.pathname + window.location.hash).toBe('/workspace#campaign=cmp_A&conversation=cnv_1')
    act(() => server.calls[server.calls.findIndex((c) => c.url === '/campaigns/cmp_A')].reply(200, campaignBody('cmp_A')))
    await waitFor(() => expect(server.scoped()).toContain(TIMELINE))
    expect(server.scoped().slice(0, 2)).toEqual(['GET /campaigns/cmp_A', 'GET /conversations/cnv_1'])
    expect(window.location.pathname + window.location.hash).toBe('/workspace#campaign=cmp_A&conversation=cnv_1')
  })

  it.each(['/workspace', '/workspace#conversation=cnv_1', '/#campaign=cmp_A&conversation=cnv_1'])(
    '%s never restores: Landing at "/" with no own key and no campaign request (T1-3)',
    async (url) => {
      const server = boot(url)
      expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
      await flush()
      expect(window.location.pathname + window.location.hash).toBe('/')
      expect(server.scoped()).toEqual([])
      await restoresAfterwards(server)
    },
  )

  it('a player lands on "/" with the keys stripped and no campaign or conversation request (T1-5)', async () => {
    const server = boot('/workspace#campaign=cmp_A&conversation=cnv_1', { role: 'player' })
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await flush()
    expect(window.location.pathname + window.location.hash).toBe('/')
    expect(server.scoped()).toEqual([])
    await restoresAfterwards(server)
  })

  it('a page that loaded signed out strips the keys before Login is drawn, and the next account never asks (T1-4)', async () => {
    const hashesWithLogin: string[] = []
    const observer = new MutationObserver(() => {
      if (screen.queryByRole('button', { name: /sign in/i }) !== null) hashesWithLogin.push(window.location.hash)
    })
    observer.observe(document.body, { subtree: true, childList: true })
    const server = boot('/workspace#campaign=cmp_A', { signedOut: true })
    await screen.findByRole('button', { name: /sign in/i })
    observer.disconnect()
    expect(hashesWithLogin[0]).toBe('')
    vi.spyOn(api, 'login').mockResolvedValue({ kind: 'ok', user: { email: 'bob@example.com', role: 'dm' } })
    await userEvent.type(screen.getByLabelText('Email'), 'bob@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'pw')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    await flush()
    expect(server.calls.filter((c) => c.url.includes('cmp_A'))).toEqual([])
    expect(window.location.hash).toBe('')
    await restoresAfterwards(server)
  })

  it('the keys follow the state, and the thread id is never read outside GM (T1-16, critic 7)', async () => {
    const server = boot('/workspace#campaign=cmp_A&conversation=cnv_1')
    await waitFor(() => expect(server.scoped()).toContain(TIMELINE))
    const before = server.calls.length
    await userEvent.click(screen.getAllByRole('button', { name: 'Sage' })[0])
    await waitFor(() => expect(window.location.hash).toBe(''))
    await flush()
    expect(server.calls.slice(before).filter((c) => c.url.includes('cnv_1'))).toEqual([])
    await userEvent.click(screen.getAllByRole('button', { name: 'GM' })[0])
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A'))
    await userEvent.click(screen.getAllByRole('button', { name: /open user menu/i })[0])
    await userEvent.click(screen.getAllByRole('button', { name: /profile/i })[0])
    expect(await screen.findByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    await flush()
    expect(window.location.pathname + window.location.hash).toBe('/profile')
  })

  it('under StrictMode each restore read is made once and applied once (T1-20)', async () => {
    const server = boot('/workspace#campaign=cmp_A&conversation=cnv_1', { strict: true })
    await waitFor(() => expect(server.scoped()).toContain(TIMELINE))
    await flush()
    expect(server.scoped().filter((l) => l === 'GET /campaigns/cmp_A' || l === 'GET /conversations/cnv_1'))
      .toEqual(['GET /campaigns/cmp_A', 'GET /conversations/cnv_1'])
  })

  it('a URL -- a cold load or a hashchange -- causes GET requests only; an explicit send is a POST (T1-24)', async () => {
    const server = boot('/workspace#campaign=cmp_A&conversation=cnv_1')
    await waitFor(() => expect(server.scoped()).toContain(TIMELINE))
    window.history.replaceState(null, '', '/workspace#campaign=cmp_B&conversation=cnv_2')
    act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
    await waitFor(() => expect(server.scoped()).toContain('GET /conversations/cnv_2'))
    await flush()
    // 1kg.6.3 (C-12c): the documents list reads the library by POST (a search never rides in a URL), and nothing else.
    expect(server.calls.filter((c) => c.method !== 'GET' && !/^\/campaigns\/cmp_[AB]\/library$/.test(c.url))).toEqual([])
    await userEvent.type(screen.getByPlaceholderText('Ask…'), 'Where is the heist?{Enter}')
    await waitFor(() => expect(server.lines()).toContain('POST /chat'))
  })
})
