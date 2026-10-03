/**
 * CreateCampaignForm.test.tsx -- the extraction contract (agent-forge-harness-30c,
 * PR-1, F-1 to F-3). `CampaignPicker.test.tsx` runs unedited beside it: that is
 * the proof moving the form changed nothing the picker's users see.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import { AppNavProvider } from './AppNav'
import { CurrentUserProvider } from './currentUser'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
import { CreateCampaignForm } from './CreateCampaignForm'
import type { IdentityChannelLike } from './identityBroadcast'

const campaign = (id: string, name: string) => ({
  schema_version: 1, campaign_id: id, name, created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
  avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
  last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
})

type Reply = { status: number; body?: unknown } | 'defer'
interface Call { url: string; method: string; reply: (r: { status: number; body?: unknown }) => void }

function stubServer(route: (call: Call) => Reply) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET',
      reply: (r) => resolve(new Response(JSON.stringify(r.body ?? {}), { status: r.status })),
    }
    calls.push(call)
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  return { fetchImpl, calls, posts: () => calls.filter((c) => c.method === 'POST') }
}

const live = {} as { c: CampaignContextValue }
function Probe(): null {
  const c = useCampaign()
  React.useLayoutEffect(() => {
    live.c = c
  })
  return null
}

async function mount(route: (call: Call) => Reply, withRef = false) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  const server = stubServer(route)
  const onCreated = vi.fn()
  const onAnnounce = vi.fn()
  const nameFieldRef = React.createRef<HTMLInputElement | HTMLTextAreaElement>()
  render(
    <AppNavProvider initialScreen="workspace" initialMode="gm">
      <CurrentUserProvider identityChannelFactory={() => {
        const channel: IdentityChannelLike = { postMessage: () => {}, close: () => {}, onmessage: null }
        return channel
      }}>
        <CampaignProvider fetchImpl={server.fetchImpl}>
          <CreateCampaignForm onCreated={onCreated} onAnnounce={onAnnounce} nameFieldRef={withRef ? nameFieldRef : undefined} />
          <Probe />
        </CampaignProvider>
      </CurrentUserProvider>
    </AppNavProvider>,
  )
  await waitFor(() => expect(live.c.enabled).toBe(true))
  return { server, onCreated, onAnnounce, nameFieldRef }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('CreateCampaignForm (30c, F-1 to F-3)', () => {
  it('F-1 a submit makes one create, and a second submit while it is in flight is no second call', async () => {
    const m = await mount((call) => (call.method === 'POST' ? 'defer' : { status: 200, body: {} }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Campaign name' }), 'The Hollow Crown')
    const form = screen.getByRole('textbox', { name: 'Campaign name' }).closest('form') as HTMLFormElement
    fireEvent.submit(form)
    await waitFor(() => expect(m.server.posts()).toHaveLength(1))
    fireEvent.submit(form)
    fireEvent.submit(form)
    await act(async () => {})
    expect(m.server.posts()).toHaveLength(1)
    // The host was told the create began exactly once: the guard is the form's, not only the provider's.
    expect(m.onAnnounce.mock.calls.filter(([text]) => text === '')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Create campaign' })).toBeDisabled()
    act(() => m.server.posts()[0].reply({ status: 201, body: campaign('cmp_new', 'The Hollow Crown') }))
    await waitFor(() => expect(m.onCreated).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'Create campaign' })).toBeEnabled()
  })

  it('F-2 a name the contract refuses reads on the field, focuses it, and is announced; no request is made', async () => {
    const m = await mount(() => ({ status: 200, body: {} }))
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    const field = screen.getByRole('textbox', { name: 'Campaign name' })
    await waitFor(() => expect(field).toHaveAttribute('aria-invalid', 'true'))
    const message = 'Give the campaign a name of 1 to 120 characters on one line.'
    expect(field).toHaveAttribute('aria-describedby', screen.getByText(message).id)
    expect(field).toHaveFocus()
    expect(m.onAnnounce).toHaveBeenLastCalledWith(message)
    expect(m.onCreated).not.toHaveBeenCalled()
    expect(m.server.posts()).toHaveLength(0)
  })

  it('F-2 a server failure keeps the typed name, says so, and focuses the field', async () => {
    const m = await mount((call) => (call.method === 'POST' ? { status: 503 } : { status: 200, body: {} }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Campaign name' }), 'The Hollow Crown')
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(m.onAnnounce).toHaveBeenLastCalledWith("Couldn't create the campaign"))
    expect(screen.getByRole('textbox', { name: 'Campaign name' })).toHaveValue('The Hollow Crown')
    expect(screen.getByRole('textbox', { name: 'Campaign name' })).toHaveFocus()
    expect(m.onCreated).not.toHaveBeenCalled()
  })

  it('F-3 a created campaign reaches onCreated, clears the field and announces it', async () => {
    const made = campaign('cmp_new', 'The Hollow Crown')
    const m = await mount((call) => (call.method === 'POST' ? { status: 201, body: made } : { status: 200, body: {} }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Campaign name' }), 'The Hollow Crown')
    await userEvent.click(screen.getByRole('button', { name: 'Create campaign' }))
    await waitFor(() => expect(m.onCreated).toHaveBeenCalledTimes(1))
    expect(m.onCreated).toHaveBeenCalledWith(made)
    expect(m.onAnnounce).toHaveBeenLastCalledWith('Campaign created')
    expect(screen.getByRole('textbox', { name: 'Campaign name' })).toHaveValue('')
    expect(screen.getByRole('textbox', { name: 'Campaign name' })).not.toHaveAttribute('aria-invalid')
    expect(m.server.posts().map((c) => c.url)).toEqual(['/campaigns'])
  })

  it('a host-supplied nameFieldRef is the name field', async () => {
    const m = await mount(() => ({ status: 200, body: {} }), true)
    expect(m.nameFieldRef.current).toBe(screen.getByRole('textbox', { name: 'Campaign name' }))
  })
})
