/**
 * LeftNavCampaign.test.tsx -- LeftNav's campaign branch (agent-forge-harness-1kg.2.5,
 * PR-2; brief sections 7.7 and 9 T2-4..T2-7, T2-11, T2-13 and the Critic's
 * items 5, 17 and 22).
 *
 * LeftNav is mounted under the REAL AppNav, CurrentUser and campaign providers
 * with `fetch` replaced by a recorder that can hold an answer back. The
 * legacy-markup snapshot (T2-6) was recorded against LeftNav as it was BEFORE
 * this PR touched it, and its snapshot file never changes afterwards.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { AppNavProvider, useAppNav, type AppNavState } from './AppNav'
import { CurrentUserProvider } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
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
interface Call { url: string; method: string; body: string | null; reply: (r: Reply) => void }
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
      reply: (r) => (r === 'network'
        ? reject(new TypeError('Failed to fetch'))
        : resolve(new Response(JSON.stringify(r.body ?? {}), { status: r.status }))),
    }
    calls.push(call)
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  return { fetchImpl, calls, lines: () => calls.map((c) => `${c.method} ${c.url}`) }
}

const live = {} as { c: CampaignContextValue; nav: AppNavState }
function Probe(): null {
  const c = useCampaign()
  const nav = useAppNav()
  React.useLayoutEffect(() => {
    live.c = c
    live.nav = nav
  })
  return null
}

interface MountOptions { campaign?: string | null; route?: Route; onNavigate?: () => void }
async function mount({ campaign = 'cmp_A', route = defaultRoute, onNavigate }: MountOptions = {}) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  window.history.replaceState(null, '', '/workspace')
  const server = stubServer(route)
  const store = new MemoryConversationStore()
  store.create('gm', 'A legacy GM question about the heist')
  store.create('gm', 'Another legacy GM question')
  store.create('sage', 'A Sage question about grappling')
  store.create('spell', 'Fireball at higher levels')
  store.create('rules', 'How does cover work')
  const view = render(
    <AppNavProvider initialScreen="workspace" initialMode="gm">
      <CurrentUserProvider identityChannelFactory={() => null}>
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
  return { server, store, view }
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

afterEach(() => {
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
