/**
 * LeftNavTavern.test.tsx -- the tavern entry in LeftNav's campaign branch
 * (agent-forge-harness-74j, brief section C.8, T-19a to T-19c).
 *
 * The harness copies `LeftNavCampaign.test.tsx:29-117`: LeftNav mounted under
 * the REAL AppNav, CurrentUser and campaign providers with `fetch` replaced
 * by a recorder.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { AppNavProvider, useAppNav, type AppNavState } from './AppNav'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
import type { IdentityChannelLike } from './identityBroadcast'
import { LeftNav } from './LeftNav'

function campaignBody(id: string, over: Record<string, unknown> = {}) {
  return {
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false, ...over,
  }
}

type Reply = { status: number; body?: unknown } | 'defer'
interface Call { url: string; method: string }
type Route = (call: Call) => Reply

const defaultRoute: Route = ({ url }) => {
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (one !== null) return { status: 200, body: campaignBody(one[1]) }
  return { status: 404, body: {} }
}

function stubServer(route: Route) {
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve) => {
    const call: Call = { url: String(input), method: init?.method ?? 'GET' }
    const answer = route(call)
    if (answer === 'defer') return
    resolve(new Response(JSON.stringify(answer.body ?? {}), { status: answer.status }))
  })) as typeof fetch
  return { fetchImpl }
}

const live = {} as { c: CampaignContextValue; nav: AppNavState; user: CurrentUserContextValue }
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

interface MountOptions {
  campaign?: string | null
  route?: Route
  onNavigate?: () => void
  mode?: 'sage' | 'spell' | 'rules' | 'gm'
  /** Renders LeftNav with no CampaignProvider above it (T-19c). */
  noProvider?: boolean
}

async function mount({ campaign = 'cmp_A', route = defaultRoute, onNavigate, mode = 'gm', noProvider = false }: MountOptions = {}) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  const server = stubServer(route)
  const store = new MemoryConversationStore()
  const inner = (
    <LeftNav onNavigate={onNavigate} />
  )
  const tree = noProvider ? inner : (
    <CampaignProvider
      fetchImpl={server.fetchImpl}
      restore={campaign === null ? null : { campaignId: campaign, conversationId: null }}
    >
      {inner}
      <Probe />
    </CampaignProvider>
  )
  render(
    <AppNavProvider initialScreen="workspace" initialMode={mode}>
      <CurrentUserProvider identityChannelFactory={() => {
        const channel: IdentityChannelLike = { postMessage: () => {}, close: () => {}, onmessage: null }
        return channel
      }}>
        <ConversationStoreProvider store={store}>
          {tree}
        </ConversationStoreProvider>
      </CurrentUserProvider>
    </AppNavProvider>,
  )
  if (!noProvider) await waitFor(() => expect(live.c.enabled).toBe(true))
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the tavern entry in LeftNav (74j, T-19a to T-19c)', () => {
  it('in GM with no campaign, a dm sees Choose a campaign; pressing it opens the tavern and closes the drawer once', async () => {
    const onNavigate = vi.fn()
    await mount({ campaign: null, onNavigate })
    const button = screen.getByRole('button', { name: 'Choose a campaign' })
    expect(button).toBeInTheDocument()
    await userEvent.click(button)
    expect(live.nav.screen).toBe('tavern')
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })

  it('offers Choose a campaign while restoring, failed and unavailable, and Switch campaign while selected', async () => {
    const cases: Array<['restoring' | 'failed' | 'unavailable', Route]> = [
      ['restoring', () => 'defer'],
      ['failed', () => ({ status: 503 })],
      ['unavailable', () => ({ status: 404 })],
    ]
    for (const [, route] of cases) {
      const onNavigate = vi.fn()
      await mount({ route, onNavigate })
      expect(screen.getByRole('button', { name: 'Choose a campaign' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Switch campaign' })).toBeNull()
      await userEvent.click(screen.getByRole('button', { name: 'Choose a campaign' }))
      expect(live.nav.screen).toBe('tavern')
      expect(onNavigate).toHaveBeenCalledTimes(1)
      vi.restoreAllMocks()
      cleanup()
    }

    const onNavigate = vi.fn()
    await mount({ onNavigate })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    expect(screen.getByRole('button', { name: 'Switch campaign' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Choose a campaign' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Switch campaign' }))
    expect(live.nav.screen).toBe('tavern')
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })

  it('Sage, Spell and Rules show no entry, with a campaign selected or not', async () => {
    for (const mode of ['sage', 'spell', 'rules'] as const) {
      await mount({ mode })
      expect(screen.queryByRole('button', { name: 'Choose a campaign' })).toBeNull()
      expect(screen.queryByRole('button', { name: 'Switch campaign' })).toBeNull()
      vi.restoreAllMocks()
      cleanup()
      await mount({ mode, campaign: null })
      expect(screen.queryByRole('button', { name: 'Choose a campaign' })).toBeNull()
      expect(screen.queryByRole('button', { name: 'Switch campaign' })).toBeNull()
      vi.restoreAllMocks()
      cleanup()
    }
  })

  it('a GM render outside a campaign provider shows neither button', async () => {
    await mount({ noProvider: true })
    expect(screen.queryByRole('button', { name: 'Choose a campaign' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Switch campaign' })).toBeNull()
  })
})
