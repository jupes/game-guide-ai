/**
 * ChatPaneCampaign.test.tsx -- a GM turn inside a campaign (agent-forge-harness-1kg.2.5,
 * PR-2; brief section 7.7, I-12, I-13, T2-1..T2-3, T2-5 and the Critic's
 * items 5, 7, 16 and 22).
 *
 * ChatPane and ModelPicker are mounted under the REAL AppNav, CurrentUser,
 * conversation-store (localStorage) and campaign providers. `post`, the
 * timeline, the history and the attachment reads are recorders; the campaign
 * and conversation routes go through a recording `fetch`. A probe OUTSIDE the
 * campaign provider reads the stored conversation id, one inside reads what
 * ChatPane is handed.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import type { ChatResult } from '../api'
import { CampaignSchema } from '../gm/contracts'
import { AppNavProvider, useAppNav, type AppNavState } from './AppNav'
import { CurrentUserProvider } from './currentUser'
import { ConversationStoreProvider, useConversationStore } from './ConversationStoreContext'
import { LocalStorageConversationStore, type ConversationStore } from './conversationStore'
import { CampaignProvider, useCampaign, type CampaignContextValue } from './campaignContext'
import { useCampaignThreads, type CampaignThreadsValue } from './campaignThreads'
import { ChatPane } from './ChatPane'
import { ModelPicker } from './ModelPicker'
import type { PostFn } from '../useChat'

const PROMPT = 'Where did the smuggler queen hide the harbour ledger?'
const campaignBody = (id: string) => CampaignSchema.parse({
  schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
  avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
  last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
})
const threadBody = (id: string, campaign = 'cmp_A') => ({
  schema_version: 1, conversation_id: id, campaign_id: campaign, title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
})

type Reply = { status: number; body?: unknown } | 'defer'
interface Call { url: string; method: string; body: unknown }

function serve(campaign: Reply = { status: 200, body: campaignBody('cmp_A') }) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const call = { url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined }
    calls.push(call)
    const answer = ((): Reply => {
      if (call.method === 'POST' && call.url === '/conversations') return { status: 201, body: threadBody(`cnv_new${calls.filter((c) => c.method === 'POST').length}`, call.body.campaign_id) }
      const one = /^\/campaigns\/(cmp_\w+)$/.exec(call.url)
      if (one !== null) return one[1] === 'cmp_A' ? campaign : { status: 200, body: campaignBody(one[1]) }
      const list = /campaign_id=(cmp_\w+)/.exec(call.url)
      if (list !== null) return { status: 200, body: { schema_version: 1, items: [threadBody(`${list[1]}-1`, list[1])], next_cursor: null } }
      return { status: 404 }
    })()
    return answer === 'defer'
      ? new Promise<Response>(() => {})
      : Promise.resolve(new Response(JSON.stringify(answer.body ?? {}), { status: answer.status }))
  }) as typeof fetch
  return { fetchImpl, calls, posts: () => calls.filter((c) => c.method !== 'GET') }
}

const live = {} as { stored: AppNavState; nav: AppNavState; c: CampaignContextValue; threads: CampaignThreadsValue; store: ConversationStore }
function Stored(): null {
  const nav = useAppNav()
  React.useLayoutEffect(() => { live.stored = nav })
  return null
}
function Probe(): null {
  const nav = useAppNav()
  const c = useCampaign()
  const threads = useCampaignThreads()
  const store = useConversationStore()
  React.useLayoutEffect(() => {
    live.store = store
    live.nav = nav
    live.c = c
    live.threads = threads
  })
  return null
}

async function mount(options: { campaign?: Reply; turns?: ChatResult[] } = {}) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  const recordFirstPrompt = vi.spyOn(LocalStorageConversationStore.prototype, 'recordFirstPrompt')
  const server = serve(options.campaign)
  const turns = [...(options.turns ?? [])]
  const post = vi.fn<PostFn>(async (_prompt, _mode, id) => turns.shift() ?? { kind: 'ok', response: { answer: 'An answer', sources: [], answerable: true, conversation_id: id } })
  const read = { history: [] as string[], timeline: [] as string[], attachments: [] as string[] }
  window.history.replaceState(null, '', '/workspace')
  render(
    <AppNavProvider initialScreen="workspace" initialMode="gm">
      <CurrentUserProvider identityChannelFactory={() => null}>
        <ConversationStoreProvider>
          <Stored />
          <CampaignProvider fetchImpl={server.fetchImpl} restore={{ campaignId: 'cmp_A', conversationId: null }}>
            <ModelPicker getModels={async () => ({ default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }] })} />
            <ChatPane
              post={post}
              loadHistory={async (id) => { read.history.push(id); return { kind: 'ok', messages: [] } }}
              loadTimeline={async (id) => { read.timeline.push(id); return { kind: 'missing' } }}
              getAttachments={async (id) => { read.attachments.push(id); return { kind: 'ok', attachments: [] } }}
            />
            <Probe />
          </CampaignProvider>
        </ConversationStoreProvider>
      </CurrentUserProvider>
    </AppNavProvider>,
  )
  await waitFor(() => expect(live.c.enabled).toBe(true))
  return { server, post, read, recordFirstPrompt }
}

async function sendTurn(text = PROMPT): Promise<void> {
  await userEvent.type(screen.getByPlaceholderText('Ask…'), `${text}{Enter}`)
}

/** No 8-character window of `text` is in any web-storage key or value (T2-1). */
function expectNothingStored(text: string): void {
  const stored = [localStorage, sessionStorage].flatMap((area) =>
    Array.from({ length: area.length }, (_, i) => `${area.key(i) ?? ''}=${area.getItem(area.key(i) ?? '') ?? ''}`))
  expect(stored.length).toBeGreaterThan(0)
  for (let i = 0; i + 8 <= text.length; i += 1) {
    for (const entry of stored) expect(entry).not.toContain(text.slice(i, i + 8))
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('the first turn of a campaign thread', () => {
  it('creates the thread once, with no title, then posts to it; nothing of the prompt is stored (T2-1, T2-2)', async () => {
    const { server, post, recordFirstPrompt } = await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    localStorage.setItem('positive-control', PROMPT.slice(0, 3))
    await sendTurn()
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_new1'))
    expect(server.posts()).toEqual([{ url: '/conversations', method: 'POST', body: { schema_version: 1, started_mode: 'gm', campaign_id: 'cmp_A' } }])
    expect(server.posts()[0].body).not.toHaveProperty('title')
    expect(post.mock.calls.map((c) => [c[0], c[2]])).toEqual([[PROMPT, 'cnv_new1']])
    expect(live.threads.threads[0]?.id).toBe('cnv_new1')
    await sendTurn('A follow-up about the ledger')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls[1][2]).toBe('cnv_new1')
    expect(server.posts()).toHaveLength(1)
    expect(recordFirstPrompt).not.toHaveBeenCalled()
    expectNothingStored(PROMPT)
    expectNothingStored('A follow-up about the ledger')
  })

  it('a legacy GM conversation left open when a campaign is chosen never receives the campaign turn (critic 5)', async () => {
    const { server, post } = await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await act(async () => { await live.c.clearCampaign() })
    const legacy = live.store.create('gm').id
    act(() => live.nav.setConversationId(legacy))
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_A')) })
    expect(live.stored.conversationId).toBe(legacy)
    expect(live.nav.conversationId).toBeNull()
    await sendTurn()
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][2]).toBe('cnv_new1')
    expect(server.posts()).toHaveLength(1)
  })

  it('reuses the created thread after a failed turn: no second create, the same id (T2-3)', async () => {
    const { server, post } = await mount({ turns: [{ kind: 'error', message: 'The oracle is silent.' }] })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await sendTurn()
    expect(await screen.findByText('The oracle is silent.')).toBeInTheDocument()
    expect(live.nav.conversationId).toBeNull()
    await sendTurn('Once more about the ledger')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls.map((c) => c[2])).toEqual(['cnv_new1', 'cnv_new1'])
    expect(server.posts()).toHaveLength(1)
  })

  it.each([
    ['restoring', 'defer', 'The campaign is still loading. Nothing was sent — try again in a moment.'],
    ['failed', { status: 503 }, "Couldn't load the campaign. Nothing was sent — use Retry, or Continue without a campaign."],
    ['unavailable', { status: 404 }, "That campaign isn't available. Nothing was sent — choose Continue without a campaign to use GM chat."],
  ] as const)('while %s, a GM send sends nothing, keeps the prompt on screen and says what to do (T2-13)', async (state, answer, message) => {
    const { server, post, recordFirstPrompt } = await mount({ campaign: answer })
    await waitFor(() => expect(live.c.selection.kind).toBe(state))
    await sendTurn()
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByText(PROMPT)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Answer failed')
    expect(post).not.toHaveBeenCalled()
    expect(server.posts()).toEqual([])
    expect(recordFirstPrompt).not.toHaveBeenCalled()
    await act(async () => { await live.c.clearCampaign() })
    await sendTurn('Now without a campaign, please')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][2]).toBeNull()
  })
})

describe('a campaign thread id stays in its campaign (I-13, critic 7)', () => {
  it('a switch, a clear and leaving GM each drop the stored id; the next Sage turn is a new conversation (T2-5)', async () => {
    const { post } = await mount()
    await waitFor(() => expect(live.threads.threads.map((t) => t.id)).toEqual(['cmp_A-1']))
    const open = async (id: string) => {
      act(() => live.nav.setConversationId(id))
      await waitFor(() => expect(live.nav.conversationId).toBe(id))
    }
    await open('cmp_A-1')
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_B')) })
    expect(live.stored.conversationId).toBeNull()
    await waitFor(() => expect(live.threads.threads.map((t) => t.id)).toEqual(['cmp_B-1']))
    await open('cmp_B-1')
    await act(async () => { await live.c.clearCampaign() })
    expect(live.stored.conversationId).toBeNull()
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_A')) })
    await waitFor(() => expect(live.threads.threads.map((t) => t.id)).toEqual(['cmp_A-1']))
    await open('cmp_A-1')
    act(() => live.nav.setMode('sage'))
    expect(live.stored.conversationId).toBeNull()
    await sendTurn('A Sage question after the campaign')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0].slice(1, 3)).toEqual(['sage', null])
  })

  it('Back to Landing and a Landing chip never read or post the thread id outside GM (critic 7)', async () => {
    const { post, read } = await mount()
    await waitFor(() => expect(live.threads.threads.map((t) => t.id)).toEqual(['cmp_A-1']))
    act(() => live.nav.setConversationId('cmp_A-1'))
    await waitFor(() => expect(read.timeline).toContain('cmp_A-1'))
    expect(read.attachments).toContain('cmp_A-1')
    const before = { history: read.history.length, attachments: read.attachments.length }
    act(() => live.nav.backToLanding())
    act(() => live.nav.enterWorkspace('sage'))
    await sendTurn('A Sage question from Landing')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls.map((c) => c[2])).toEqual([null])
    expect(read.history.slice(before.history)).not.toContain('cmp_A-1')
    expect(read.attachments.slice(before.attachments)).not.toContain('cmp_A-1')
  })
})

describe('ModelPicker on a campaign thread (critic 16)', () => {
  it('is disabled while the active conversation is not in the local store, and enabled for a stored one', async () => {
    await mount()
    await waitFor(() => expect(live.threads.threads.map((t) => t.id)).toEqual(['cmp_A-1']))
    act(() => live.nav.setConversationId('cmp_A-1'))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toBeDisabled())
    await act(async () => { await live.c.clearCampaign() })
    act(() => live.nav.setConversationId(live.store.create('gm').id))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toBeEnabled())
  })
})
