/**
 * CampaignPicker.test.tsx -- the picker's §12.2 states, its one announcer, the
 * create form and its account-keyed state (agent-forge-harness-1kg.2.5, PR-2;
 * brief sections 9 T2-8..T2-10 and 11, the Critic's items 15 and 21).
 *
 * Mounted under the REAL AppNav, CurrentUser and campaign providers; `fetch`
 * is a recorder that can hold an answer back, and another tab's identity
 * signal is a fake channel.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { AppNavProvider } from './AppNav'
import { CurrentUserContext, CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './currentUser'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
import { CampaignPicker } from './CampaignPicker'
import type { IdentityChannelLike } from './identityBroadcast'

const campaign = (id: string, over: Record<string, unknown> = {}) => ({
  schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
  avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
  last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false, ...over,
})
const page = (items: unknown[], next: string | null = null) => ({ schema_version: 1, items, next_cursor: next })

type Reply = { status: number; body?: unknown } | 'network'
interface Call { url: string; method: string; body: string | null; reply: (r: Reply) => void }

function stubServer(route: (call: Call) => Reply | 'defer') {
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

const live = {} as { c: CampaignContextValue; userId: string }
function Probe(): null {
  const c = useCampaign()
  const { user } = useCurrentUser()
  React.useLayoutEffect(() => {
    live.c = c
    live.userId = user.id
  })
  return null
}

async function mount(route: (call: Call) => Reply | 'defer') {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  const server = stubServer(route)
  const channels: IdentityChannelLike[] = []
  const onSelected = vi.fn()
  const announced: string[] = []
  const observer = new MutationObserver(() => {
    const text = document.querySelector('.campaign-picker__status')?.textContent ?? ''
    if (text !== '' && text !== announced.at(-1)) announced.push(text)
  })
  observer.observe(document.body, { subtree: true, childList: true, characterData: true })
  const view = render(
    <AppNavProvider initialScreen="workspace" initialMode="gm">
      <CurrentUserProvider identityChannelFactory={() => {
        const channel: IdentityChannelLike = { postMessage: () => {}, close: () => {}, onmessage: null }
        channels.push(channel)
        return channel
      }}>
        <CampaignProvider fetchImpl={server.fetchImpl}>
          <CampaignPicker onSelected={onSelected} />
          <Probe />
        </CampaignProvider>
      </CurrentUserProvider>
    </AppNavProvider>,
  )
  const status = view.container.querySelector('[role="status"]')
  const statusAtMount = status?.textContent
  await waitFor(() => expect(live.c.enabled).toBe(true))
  /** Another tab signed in as `email`: this tab's background re-check sees it. */
  const switchTo = async (email: string) => {
    vi.mocked(api.getMe).mockResolvedValue({ kind: 'ok', user: { email, role: 'dm' } })
    act(() => {
      for (const c of channels) c.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    })
    await waitFor(() => expect(live.userId).toBe(email))
  }
  return { server, onSelected, announced, statusAtMount, switchTo, stop: () => observer.disconnect() }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the picker states (§12.2, T2-8, T2-9)', () => {
  it('loads on mount: skeleton and visible text, then a labelled list of buttons; one announcer, empty at mount', async () => {
    const m = await mount(({ url }) => (url === '/campaigns' ? 'defer' : { status: 404 }))
    expect(m.statusAtMount).toBe('')
    expect(document.querySelectorAll('[role="status"], [aria-live]')).toHaveLength(1)
    expect(screen.getByText('Loading campaigns…', { selector: 'p:not([role])' })).toBeInTheDocument()
    expect(document.querySelectorAll('.campaign-picker__skeleton')).toHaveLength(2)
    act(() => m.server.calls[0].reply({ status: 200, body: page([campaign('cmp_A', { badge: 'live' }), campaign('cmp_B', { concluded_at: '2026-09-20T00:00:00Z' })]) }))
    const list = await screen.findByRole('list', { name: 'Your campaigns' })
    expect(Array.from(list.querySelectorAll('button'), (b) => b.textContent)).toEqual(['Name of cmp_A LIVE', 'Name of cmp_B Concluded'])
    expect(screen.getByRole('button', { name: 'Name of cmp_A LIVE' })).toHaveAttribute('aria-pressed', 'false')
    expect(document.querySelectorAll('.campaign-picker__skeleton')).toHaveLength(0)
    m.stop()
    expect(m.announced).toEqual(['Loading campaigns…', 'Campaigns loaded'])
  })

  it('an empty list invites the first campaign', async () => {
    await mount(() => ({ status: 200, body: page([]) }))
    expect(await screen.findByText('Create your first campaign — only a name is required')).toBeInTheDocument()
    expect(screen.queryByRole('list')).toBeNull()
  })

  it('a failure names Retry, which stays mounted and focused through its request, then hands focus to the heading (M2-8)', async () => {
    let reads = 0
    const m = await mount(({ url }) => {
      if (url !== '/campaigns') return { status: 404 }
      reads += 1
      return reads === 1 ? { status: 503 } : 'defer'
    })
    const retry = await screen.findByRole('button', { name: 'Retry' })
    expect(screen.getAllByText("Couldn't load campaigns").length).toBeGreaterThan(0)
    await userEvent.click(retry)
    expect(retry).toBeInTheDocument()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).toHaveFocus()
    await userEvent.click(retry)
    expect(m.server.lines()).toEqual(['GET /campaigns', 'GET /campaigns'])
    act(() => m.server.calls[1].reply({ status: 200, body: page([campaign('cmp_A')]) }))
    expect(await screen.findByRole('heading', { name: 'Your campaigns' })).toHaveFocus()
    m.stop()
    expect(m.announced).toEqual(['Loading campaigns…', "Couldn't load campaigns", 'Loading campaigns…', 'Campaigns loaded'])
  })

  it('Load more appends in server order without reordering what is shown', async () => {
    const m = await mount(({ url }) => (url === '/campaigns'
      ? { status: 200, body: page([campaign('cmp_C'), campaign('cmp_B')], 'next_1') }
      : { status: 200, body: page([campaign('cmp_B'), campaign('cmp_A')]) }))
    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull())
    expect(m.server.lines().at(-1)).toBe('GET /campaigns?cursor=next_1')
    expect(Array.from(screen.getByRole('list').querySelectorAll('button'), (b) => b.textContent)).toEqual(['Name of cmp_C', 'Name of cmp_B', 'Name of cmp_A'])
    expect(screen.getByRole('heading', { name: 'Your campaigns' })).toHaveFocus()
  })

  it('choosing a campaign selects it and tells the host', async () => {
    const m = await mount(() => ({ status: 200, body: page([campaign('cmp_A'), campaign('cmp_B')]) }))
    await userEvent.click(await screen.findByRole('button', { name: 'Name of cmp_B' }))
    await waitFor(() => expect(m.onSelected).toHaveBeenCalledTimes(1))
    expect(m.onSelected.mock.calls[0][0]).toMatchObject({ campaign_id: 'cmp_B' })
    expect(screen.getByRole('button', { name: 'Name of cmp_B' })).toHaveAttribute('aria-pressed', 'true')
    expect(m.server.lines()).toEqual(['GET /campaigns'])
  })
})

describe('a provider mounted for an account already signed in', () => {
  it('is enabled at once: the picker reads the list (the shape a story harness, or a later mount, has)', async () => {
    const server = stubServer(() => ({ status: 200, body: page([campaign('cmp_A')]) }))
    const signedIn: CurrentUserContextValue = {
      user: { id: 'ada@example.com', displayName: 'Ada', initials: 'A', role: 'dm', signOut: async () => true, editProfile: () => {} },
      authStatus: 'authenticated', retryAuthCheck: () => {}, signIn: () => {}, setDisplayName: () => {}, setAvatarTone: () => {},
    }
    render(
      <CurrentUserContext.Provider value={signedIn}>
        <CampaignProvider fetchImpl={server.fetchImpl}><CampaignPicker /></CampaignProvider>
      </CurrentUserContext.Provider>,
    )
    expect(await screen.findByRole('button', { name: 'Name of cmp_A' })).toBeInTheDocument()
    expect(server.lines()).toEqual(['GET /campaigns'])
  })
})

describe('the create form (T2-10, critic 15)', () => {
  it('a refused name is programmatic: aria-invalid, described, focused, and no request (M2-10)', async () => {
    const m = await mount(() => ({ status: 200, body: page([]) }))
    const field = await screen.findByLabelText('Campaign name')
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(field).toHaveAttribute('aria-invalid', 'true'))
    expect(field).toHaveAccessibleDescription('Give the campaign a name of 1 to 120 characters on one line.')
    expect(field).toHaveFocus()
    expect(m.server.lines()).toEqual(['GET /campaigns'])
  })

  it('a failed create is sent once, announced, keeps the name, re-reads the list with a GET, and never retries', async () => {
    let posts = 0
    const m = await mount(({ method }) => {
      if (method !== 'POST') return { status: 200, body: page([]) }
      posts += 1
      return posts === 1 ? 'defer' : { status: 201, body: campaign('cmp_new', { name: 'The Drowned Crown' }) }
    })
    const field = await screen.findByLabelText('Campaign name')
    await userEvent.type(field, 'The Drowned Crown')
    const create = screen.getByRole('button', { name: 'Create campaign' })
    await userEvent.click(create)
    expect(create).toBeDisabled()
    await userEvent.type(field, '{Enter}')
    expect(m.server.lines().filter((l) => l.startsWith('POST'))).toHaveLength(1)
    act(() => m.server.calls.find((c) => c.method === 'POST')?.reply({ status: 503 }))
    await waitFor(() => expect(field).toHaveFocus())
    expect(field).toHaveAccessibleDescription("Couldn't create the campaign")
    expect(field).toHaveValue('The Drowned Crown')
    await waitFor(() => expect(m.server.lines()).toEqual(['GET /campaigns', 'POST /campaigns', 'GET /campaigns']))
    expect(m.announced.at(-1)).toBe("Couldn't create the campaign")
    await userEvent.click(create)
    await waitFor(() => expect(m.onSelected).toHaveBeenCalledTimes(1))
    expect(field).toHaveValue('')
    expect(screen.getByRole('button', { name: 'The Drowned Crown' })).toHaveAttribute('aria-pressed', 'true')
    m.stop()
    expect(m.announced).toEqual(['Loading campaigns…', 'Campaigns loaded', "Couldn't create the campaign", 'Campaign created'])
    expect(JSON.parse(m.server.calls.filter((c) => c.method === 'POST')[1].body ?? '{}')).toStrictEqual({ schema_version: 1, name: 'The Drowned Crown' })
  })

  it('the typed name belongs to one account: another tab signing in as bob empties it (critic 21)', async () => {
    const m = await mount(() => ({ status: 200, body: page([]) }))
    await userEvent.type(await screen.findByLabelText('Campaign name'), 'Ada’s secret campaign')
    expect(screen.getByLabelText('Campaign name')).toHaveValue('Ada’s secret campaign')
    await m.switchTo('bob@example.com')
    await waitFor(() => expect(screen.getByLabelText('Campaign name')).toHaveValue(''))
    expect(screen.queryByDisplayValue('Ada’s secret campaign')).toBeNull()
  })
})
