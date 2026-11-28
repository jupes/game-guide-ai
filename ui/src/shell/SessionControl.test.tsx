/**
 * SessionControl.test.tsx -- what the GM sees and presses (agent-forge-harness-
 * 1kg.2.5.3; brief sections 9 and 11: T4-3, T4-5, the one live region, Critic 23c).
 */

import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { tableSessionBody } from '../testing/workbenchHarness'
import { SessionControl } from './SessionControl'
import { TableSessionProvider, type TableSessionValue, type TableSessionView } from './tableSession'
import { EndCourier } from './tableSessionApi'

const LIVE: TableSessionView = {
  sessionId: 'tss_1',
  campaignId: 'cmp_A',
  state: 'live',
  startedAt: new Date(Date.now() - 60_000).toISOString(),
  endsAt: new Date(Date.now() + 3_600_000).toISOString(),
  endedAt: null,
}

function valueOf(overrides: Partial<TableSessionValue> = {}): TableSessionValue {
  return {
    state: 'none',
    session: null,
    pending: null,
    endRetrying: false,
    problem: null,
    liveSession: null,
    start: vi.fn(async () => 'started' as const),
    end: vi.fn(async () => 'ended' as const),
    retry: vi.fn(async () => 'skipped' as const),
    ...overrides,
  }
}

const LIVE_REGIONS = '[role="status"],[role="alert"],[role="log"],[aria-live]'

describe('SessionControl', () => {
  it('mounts one live region, empty; given the pane announcer it mounts none', async () => {
    const { container, unmount } = render(<SessionControl value={valueOf()} />)
    const regions = container.querySelectorAll(LIVE_REGIONS)
    expect(regions).toHaveLength(1)
    expect(regions[0]).toHaveTextContent('')
    unmount()
    const announce = vi.fn()
    const hosted = render(<SessionControl value={valueOf()} announce={announce} />)
    expect(hosted.container.querySelectorAll(LIVE_REGIONS)).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Start session' }))
    expect(announce.mock.calls).toEqual([['Starting…'], ['Session started']])
  })

  it('announces a Start at its press and at its end, and a failure as a failure', async () => {
    const start = vi.fn(async () => 'failed' as const)
    render(<SessionControl value={valueOf({ start })} />)
    expect(screen.getByText('No session running')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Start session' }))
    expect(start).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status')).toHaveTextContent("Couldn't start the session")
  })

  it.each<[string, Partial<TableSessionValue>]>([
    ['an End waiting to retry', { pending: 'end', endRetrying: true }],
    ['a Start answer pending', { pending: 'start' }],
    ['a failed status re-read', { state: 'failed', problem: { kind: 'load_failed' } }],
    ['a status read in flight', { state: 'loading' }],
  ])('T4-3: End is enabled during %s', async (_label, overrides) => {
    const end = vi.fn(async () => 'ended' as const)
    render(<SessionControl value={valueOf({ state: 'live', session: LIVE, end, ...overrides })} />)
    const button = screen.getByRole('button', { name: 'End session' })
    expect(button).toBeEnabled()
    await userEvent.click(button)
    expect(end).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status')).toHaveTextContent('Session ended')
  })

  it('says an End is still being tried, and offers no Start while live', () => {
    render(<SessionControl value={valueOf({ state: 'live', session: LIVE, pending: 'end', endRetrying: true })} />)
    expect(screen.getByText(/Couldn't end the session yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start session' })).toBeNull()
  })

  it('withdraws the live indicator at expiry and keeps End available until the server confirms', () => {
    render(<SessionControl value={valueOf({ state: 'live', session: LIVE, liveSession: null })} />)
    expect(screen.getByText('Session end time has passed')).toBeVisible()
    expect(screen.queryByText('Live')).toBeNull()
    expect(screen.queryByText(/Live until/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Share table' })).toBeNull()
    expect(screen.getByRole('button', { name: 'End session' })).toBeEnabled()
  })

  it('Critic 23c: a refused Start offers no Retry', () => {
    render(<SessionControl value={valueOf({ problem: { kind: 'start_refused' } })} />)
    expect(screen.getByText("Couldn't start the session")).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Start session' })).toBeEnabled()
  })

  it('shows live_elsewhere with a Start and no End', () => {
    render(<SessionControl value={valueOf({ state: 'ended', problem: { kind: 'live_elsewhere' } })} />)
    expect(screen.getByText(/live in another campaign/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'End session' })).toBeNull()
  })

  it('a throttled Start names its wait and retries through retry()', async () => {
    const retry = vi.fn(async () => 'started' as const)
    render(<SessionControl value={valueOf({ problem: { kind: 'throttled', retryAfterS: 40 }, retry })} />)
    expect(screen.getByText(/Try again in 40 seconds/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(retry).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status')).toHaveTextContent('Session started')
  })

  it('renders nothing but its empty live region while idle', () => {
    const { container } = render(<SessionControl value={valueOf({ state: 'idle' })} />)
    expect(screen.queryByRole('group')).toBeNull()
    expect(container.querySelectorAll(LIVE_REGIONS)).toHaveLength(1)
  })

  it('starts and ends the selected campaign session through the merged provider', async () => {
    const route = '/campaigns/cmp_A/table-session'
    const actions: Array<{ path: string; body: unknown }> = []
    let releaseEnd = () => {}
    const heldEnd = new Promise<void>((resolve) => { releaseEnd = resolve })
    const active = {
      schema_version: 1,
      session_id: LIVE.sessionId,
      campaign_id: LIVE.campaignId,
      state: 'live',
      gen: 1,
      audio_epoch: 0,
      reveal_epoch: 0,
      started_at: LIVE.startedAt,
      ends_at: LIVE.endsAt,
      ended_at: null,
      audio: false,
      screens: [],
    }
    const fetchImpl = vi.fn<typeof fetch>(async (input, init) => {
      if (init?.method !== 'POST') return Response.json({ schema_version: 1, session: null })
      const body = JSON.parse(String(init.body))
      actions.push({ path: String(input), body })
      if (body.action === 'end') await heldEnd
      const session = body.action === 'end'
        ? { ...active, state: 'ended', ended_at: new Date().toISOString() }
        : active
      return Response.json({ schema_version: 1, session })
    })
    const user = userEvent.setup()
    render(
      <TableSessionProvider scope={{ campaignId: 'cmp_A', key: 'scope-1' }} fetchImpl={fetchImpl} courier={new EndCourier()}>
        <SessionControl />
      </TableSessionProvider>,
    )
    const start = await screen.findByRole('button', { name: 'Start session' })
    expect(screen.queryByRole('button', { name: 'Share table' })).toBeNull()
    await user.click(start)
    expect(await screen.findByText('Live')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Share table' }))
    expect(screen.getByRole('textbox', { name: 'Table address' })).toHaveValue(`${location.origin}/table?campaign_id=cmp_A`)
    await user.click(screen.getByRole('button', { name: 'End session' }))
    expect(screen.queryByRole('textbox', { name: 'Table address' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Share table' })).toBeNull()
    expect(screen.getByRole('button', { name: 'End session' })).toBeEnabled()
    await act(async () => { releaseEnd() })
    expect(await screen.findByText(/The last session ended at/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'End session' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Share table' })).toBeNull()
    expect(actions).toEqual([
      { path: route, body: { schema_version: 1, action: 'start', command_id: expect.any(String) } },
      { path: route, body: { schema_version: 1, action: 'end', command_id: expect.any(String), session_id: LIVE.sessionId } },
    ])
  })

  it('withdraws an open address as soon as the campaign changes, before the new session is known', async () => {
    let releaseB = () => {}
    const heldB = new Promise<void>((resolve) => { releaseB = resolve })
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      if (String(input) === '/campaigns/cmp_A/table-session') return Response.json(tableSessionBody())
      await heldB
      return Response.json(tableSessionBody({ campaign_id: 'cmp_B', session_id: 'ses_B' }))
    })
    const courier = new EndCourier()
    const controls = (campaignId: string) => (
      <TableSessionProvider scope={{ campaignId, key: campaignId }} fetchImpl={fetchImpl} courier={courier}>
        <SessionControl />
      </TableSessionProvider>
    )
    const user = userEvent.setup()
    const view = render(controls('cmp_A'))
    await user.click(await screen.findByRole('button', { name: 'Share table' }))
    expect(screen.getByRole('textbox', { name: 'Table address' })).toHaveValue(`${location.origin}/table?campaign_id=cmp_A`)
    view.rerender(controls('cmp_B'))
    expect(screen.queryByRole('button', { name: 'Share table' })).toBeNull()
    expect(screen.queryByRole('textbox', { name: 'Table address' })).toBeNull()
    await act(async () => { releaseB() })
    expect(screen.queryByRole('textbox', { name: 'Table address' })).toBeNull()
    await user.click(await screen.findByRole('button', { name: 'Share table' }))
    expect(screen.getByRole('textbox', { name: 'Table address' })).toHaveValue(`${location.origin}/table?campaign_id=cmp_B`)
  })

  it('T4-5: through the real provider, a token in an answer never reaches the page', async () => {
    const calls: string[] = []
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? 'GET'} ${String(input)}`)
      const session = { schema_version: 1, session_id: 'tss_1', campaign_id: 'cmp_A', state: 'live', gen: 1,
        audio_epoch: 0, reveal_epoch: 0, started_at: LIVE.startedAt, ends_at: LIVE.endsAt, ended_at: null,
        audio: false, screens: [], token: 'secret-grant' }
      return new Response(JSON.stringify({ schema_version: 1, session, token: 'secret-link' }), { status: 200 })
    }) as typeof fetch
    const { container } = render(
      <TableSessionProvider scope={{ campaignId: 'cmp_A', key: 'scope-1' }} fetchImpl={fetchImpl} courier={new EndCourier()}>
        <SessionControl />
      </TableSessionProvider>,
    )
    const group = await screen.findByRole('group', { name: 'Table session' })
    expect(within(group).getByText('Live')).toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/secret|token/)
    expect(calls).toEqual(['GET /campaigns/cmp_A/table-session'])
  })
})
