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
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../api'
import type { ChatResult } from '../api'
import { CampaignSchema, TOOL_IDS, parseTimelineEntry } from '../gm/contracts'
import { documentFixture } from '../gm/documentFixtures'
import * as canvas from './canvasContext'
import { documentResult, errorInfo, toolInvocation } from '../gm/laneFixtures'
import type { LoadTimelinePageFn } from '../gm/gmTimeline'
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
const NOT_STARTED = "Couldn't start a GM thread. Nothing was sent — try again."
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

function serve(campaign: Reply = { status: 200, body: campaignBody('cmp_A') }, create?: Reply) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const call = { url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined }
    calls.push(call)
    const answer = ((): Reply => {
      if (call.method === 'POST' && call.url === '/conversations') return create ?? { status: 201, body: threadBody(`cnv_new${calls.filter((c) => c.method === 'POST').length}`, call.body.campaign_id) }
      if (call.method === 'GET' && call.url === '/campaigns') return { status: 200, body: { schema_version: 1, items: [campaignBody('cmp_A'), campaignBody('cmp_B')], next_cursor: null } }
      const one = /^\/campaigns\/(cmp_\w+)$/.exec(call.url)
      if (one !== null) return one[1] === 'cmp_A' ? campaign : one[1] === 'cmp_Gone' ? { status: 404 } : { status: 200, body: campaignBody(one[1]) }
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

const availableTools = Object.fromEntries(TOOL_IDS.map((tool) => [
  tool, tool === 'npc' || tool === 'hooks' ? null : 'This tool is unavailable.',
]))
const jsonReply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

async function mount(options: {
  campaign?: Reply
  create?: Reply
  turns?: ChatResult[]
  tools?: typeof fetch
  loadTimeline?: LoadTimelinePageFn
} = {}) {
  vi.spyOn(api, 'getMe').mockResolvedValue({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  const recordFirstPrompt = vi.spyOn(LocalStorageConversationStore.prototype, 'recordFirstPrompt')
  const server = serve(options.campaign, options.create)
  vi.stubGlobal('fetch', options.tools ?? (async () => jsonReply(availableTools)))
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
              loadTimeline={options.loadTimeline ?? (async (id) => { read.timeline.push(id); return { kind: 'missing' } })}
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
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

it('draws an acknowledged cue after the live turn in the current GM thread only', async () => {
  const cueId = 'cue_LiveAttachment00000001'
  const when = '2026-01-01T00:00:00Z'
  await mount({
    tools: async (url) => String(url) === `/campaigns/cmp_A/cues/${cueId}`
      ? jsonReply({
        schema_version: 1, cue_id: cueId, campaign_id: 'cmp_A', asset_id: 'ast_LiveAttachment00000001',
        title: 'Rain on the roof', kind: 'one_shot', duration_ms: 1200, archived: false, created_at: when, updated_at: when,
      })
      : jsonReply(availableTools),
  })
  await sendTurn()
  await screen.findByText('An answer')
  await waitFor(() => expect(live.nav.conversationId).not.toBeNull())
  const thread = live.nav.conversationId
  if (thread === null) throw new Error('Expected a campaign thread')
  const entry = {
    schema_version: 1, entry_kind: 'attached_cue', entry_id: 'ent_LiveAttachment00000001', cue_id: cueId, created_at: when,
  } as const
  act(() => live.threads.reportCueAttachment('cnv_another_thread', entry))
  expect(screen.queryByText('AUDIO CUE')).toBeNull()
  act(() => live.threads.reportCueAttachment(thread, entry))
  const badge = await screen.findByText('AUDIO CUE')
  const answer = screen.getByText('An answer')
  expect(answer.compareDocumentPosition(badge) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(await screen.findByRole('button', { name: 'Play preview of Rain on the roof' })).toBeVisible()
})

describe('private images in the GM thread', () => {
  const when = '2026-01-01T00:00:00Z'
  const asset = {
    schema_version: 1, asset_id: 'ast_map', campaign_id: 'cmp_A', kind: 'image', state: 'ready',
    media_type: 'image/png', size_bytes: 4, width: 80, height: 40, alt: 'The hidden ferry crossing',
    duration_ms: null, failure: null, created_at: when, updated_at: when,
  }
  const collection = '/campaigns/cmp_A/assets'
  const path = collection + '/ast_map'
  const entry = { schema_version: 1, entry_id: 'ent_map', entry_kind: 'attached_image', created_at: when, asset_id: asset.asset_id }
  function world(attach: (body: Record<string, unknown>) => Promise<Response>) {
    const calls: { path: string; init?: RequestInit }[] = []
    const request: typeof fetch = async (input, init) => {
      const url = String(input)
      calls.push({ path: url, init })
      if (url.endsWith('/tool-availability')) return jsonReply(availableTools)
      if (url === collection + '/upload-capability') return new Response(null, { status: 204 })
      if (url === collection) return jsonReply({ ...asset, state: 'uploading', width: null, height: null }, 201)
      if (url === path + '/bytes' || url === path + '/status') return jsonReply(asset)
      if (url === path + '/attach') return attach(JSON.parse(String(init?.body)))
      return jsonReply({}, 404)
    }
    return { calls, request }
  }
  async function openForm() {
    await userEvent.click(await screen.findByRole('button', { name: 'Attach image' }))
    const form = within(screen.getByRole('dialog', { name: 'Attach image to GM thread' }))
    await form.findByRole('combobox', { name: 'Use as' })
    return form
  }
  async function chooseFile(form: ReturnType<typeof within>) {
    await userEvent.upload(form.getByLabelText('Image file'), new File(['data'], 'private.png', { type: 'image/png' }))
    await userEvent.type(form.getByLabelText('Image description'), asset.alt)
  }
  it('retries just the attachment, opens its new thread at once, and carries the unsent GM draft with it', async () => {
    let fail = true
    const { request, calls } = world(async body => {
      if (fail) return jsonReply({}, 503)
      return jsonReply({ ...entry, use: body.use })
    })
    const { server } = await mount({ tools: request, loadTimeline: () => new Promise(() => {}) })
    const draft = 'What if the ferry never arrives?'
    await userEvent.type(await screen.findByRole('textbox', { name: 'Message' }), draft)
    const form = await openForm()
    await userEvent.selectOptions(form.getByRole('combobox', { name: 'Use as' }), 'map')
    await chooseFile(form)
    const add = form.getByRole('button', { name: 'Add to GM thread' })
    await userEvent.click(add)
    expect(await form.findByRole('alert')).toHaveTextContent('Try again with the same image')
    fail = false
    await userEvent.click(add)
    expect(await screen.findByRole('img', { name: asset.alt })).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue(draft)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(server.posts().filter(call => call.url === '/conversations')).toHaveLength(1)
    expect(calls.filter(call => call.path === collection)).toHaveLength(1)
    expect(calls.filter(call => call.path === path + '/bytes')).toHaveLength(1)
    expect(calls.filter(call => call.path === path + '/attach').map(call => JSON.parse(String(call.init?.body))))
      .toEqual(Array(2).fill({ schema_version: 1, conversation_id: live.nav.conversationId, use: 'map' }))
    expect(calls.some(call => call.init?.method === 'DELETE' || call.path.startsWith('/table/'))).toBe(false)
  })

  it('can attach the saved image of the open document without creating or uploading media bytes', async () => {
    const doc = documentFixture('npc')
    const state = { doc: { kind: 'open', document: { ...doc, campaign_id: 'cmp_A',
      data: { ...doc.data, portrait: { asset_id: asset.asset_id, media_type: 'image', alt: asset.alt } } } },
      view: 'chat', documentsVersion: 0, guardDialog: null, announcement: '', announcementTick: 0,
    } as const
    vi.spyOn(canvas, 'useCanvasState').mockReturnValue(state)
    const { calls, request } = world(async body => jsonReply({ ...entry, use: body.use }))
    await mount({ tools: request })
    const form = await openForm()
    await chooseFile(form)
    const source = form.getByRole('checkbox', { name: 'Use the open document’s image' })
    await userEvent.click(source)
    await userEvent.click(source)
    expect(form.getByLabelText('Image file')).toHaveValue('')
    expect(form.getByLabelText('Image description')).toHaveValue('')
    await userEvent.click(form.getByRole('button', { name: 'Add to GM thread' }))
    expect(form.getByRole('alert')).toHaveTextContent(/choose a png/i)
    await userEvent.click(source)
    expect(form.getByText(asset.alt)).toBeVisible()
    await userEvent.click(form.getByRole('button', { name: 'Add to GM thread' }))
    expect(await screen.findByRole('img', { name: asset.alt })).toBeVisible()
    expect(calls.filter(call => call.init?.method === 'POST').map(call => call.path)).toEqual([path + '/attach'])
    expect(calls.some(call => call.init?.method === 'PUT' || call.init?.method === 'DELETE')).toBe(false)
  })

  it('keeps the upload unavailable until a successful capability check', async () => {
    let enabled = false
    const request = vi.fn<typeof fetch>(async input => String(input).endsWith('/upload-capability')
      ? new Response(null, { status: enabled ? 204 : 404 }) : jsonReply(availableTools))
    await mount({ tools: request })
    await userEvent.click(await screen.findByRole('button', { name: 'Attach image' }))
    const dialog = within(screen.getByRole('dialog'))
    expect(await dialog.findByText(/attachments are unavailable/)).toBeVisible()
    expect(dialog.queryByLabelText('Image file')).toBeNull()
    enabled = true
    await userEvent.click(dialog.getByRole('button', { name: 'Check again' }))
    expect(await dialog.findByLabelText('Image file')).toBeVisible()
    expect(request.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })

  it('does not resurrect a submitted image or move the user back after switching channels', async () => {
    let settle!: (response: Response) => void
    const { calls, request } = world(() => new Promise(resolve => { settle = resolve }))
    await mount({ tools: request })
    const form = await openForm()
    await chooseFile(form)
    await userEvent.click(form.getByRole('button', { name: 'Add to GM thread' }))
    const post = calls.find(call => call.path === path + '/attach')
    expect(post).toBeDefined()
    act(() => live.nav.setMode('sage'))
    expect(post?.init?.signal?.aborted).toBe(true)
    await act(async () => { settle(jsonReply({ ...entry, use: 'portrait' })) })
    expect(screen.queryByRole('img', { name: asset.alt })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(live.nav.mode).toBe('sage')
    expect(calls.some(call => call.init?.method === 'DELETE')).toBe(false)
  })
})

describe('the GM tool composer', () => {
  it('arms a draft, keeps it in GM through a channel switch, and cites the live result only on Send', async () => {
    const posted: Record<string, unknown>[] = []
    let finish: (response: Response) => void = () => {}
    const toolFetch: typeof fetch = async (url, init) => {
      if (String(url).endsWith('/tool-availability')) return jsonReply(availableTools)
      if (init?.method !== 'POST') return jsonReply({}, 404)
      const request = JSON.parse(String(init.body)) as Record<string, unknown>
      posted.push(request)
      if (posted.length > 1) return jsonReply({ detail: errorInfo({ code: 'invalid', retryable: false }) }, 422)
      return new Promise((resolve) => { finish = resolve })
    }
    const { post } = await mount({ tools: toolFetch })
    const rail = await screen.findByRole('group', { name: 'GM tools' })
    const button = within(rail).getByRole('button', { name: 'NPC' })
    await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled'))
    await userEvent.click(button)
    await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), 'a rain-soaked ferryman')
    expect(posted).toEqual([])

    await act(async () => { live.stored.setMode('sage') })
    expect(screen.getByPlaceholderText('Ask…')).toHaveValue('')
    await act(async () => { live.stored.setMode('gm') })
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('/npc a rain-soaked ferryman')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run NPC tool' })).toBeEnabled())
    await userEvent.click(screen.getByRole('button', { name: 'Run NPC tool' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    await screen.findByText('Writing the dossier…')
    await sendTurn('An unrelated question for the GM')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))

    await act(async () => finish(jsonReply(toolInvocation({
      invocation_id: posted[0].invocation_id,
      entry_id: 'entry_ferryman',
      status: 'done',
      result: documentResult({ suggestions: [
        { tool_id: 'hooks', label: 'Write three hooks', brief: 'around the ferry' },
        { tool_id: 'portrait', label: 'Unavailable portrait' },
      ] }),
    }))))
    expect(await screen.findByText('Ondrey the Ferryman')).toBeInTheDocument()
    expect(screen.queryByText('Unavailable portrait')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Write three hooks' }))
    expect(posted).toHaveLength(1)
    const field = screen.getByRole('textbox', { name: 'Message' })
    expect(field).toHaveFocus()
    expect(field).toHaveValue('/hook around the ferry')
    await userEvent.click(screen.getByRole('button', { name: 'Run Hooks tool' }))
    await waitFor(() => expect(posted).toHaveLength(2))
    expect(posted[1]).toMatchObject({ tool_id: 'hooks', source_entry_id: 'entry_ferryman' })
  })

  it('adopts a hydrated working run, recovers a failed cancel, then retries the original request', async () => {
    const invocation = toolInvocation({ status: 'working' })
    const history = parseTimelineEntry({
      schema_version: 1, entry_kind: 'tool', entry_id: 'entry_hydrated',
      source_entry_id: 'entry_prior', created_at: invocation.created_at,
      brief: 'the night guard', invocation,
    })
    const writes: { url: string; body: unknown }[] = []
    const toolFetch: typeof fetch = async (input, init) => {
      const url = String(input)
      if (url.endsWith('/tool-availability')) return jsonReply(availableTools)
      if (init?.method === 'POST') {
        writes.push({ url, body: JSON.parse(String(init.body)) })
        if (url.endsWith('/cancel')) return jsonReply({}, 502)
        return jsonReply(toolInvocation({ status: 'done', attempt: 2, result: documentResult() }))
      }
      return jsonReply(toolInvocation({
        status: 'failed', error: errorInfo(), updated_at: '2026-09-21T12:00:00Z',
      }))
    }
    const { server } = await mount({
      tools: toolFetch,
      loadTimeline: async (id) => ({ kind: 'ok', page: { conversation_id: id, items: [history], next_cursor: null } }),
    })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await sendTurn('Open a conversation for the GM')
    await screen.findByText('Checking on NPC…')
    expect(writes).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(writes[0]?.url).toMatch(/\/cancel$/))
    await userEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Ondrey the Ferryman')).toBeInTheDocument()
    expect(writes[1].body).toMatchObject({
      invocation_id: invocation.invocation_id, tool_id: 'npc',
      conversation_id: live.nav.conversationId, campaign_id: 'cmp_A', brief: 'the night guard',
      source_entry_id: 'entry_prior',
    })
    expect(server.posts()).toHaveLength(1)
  })
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

  it('once a turn is answered its thread is opened, so New conversation makes a second thread, not a reuse (I-12)', async () => {
    const { server, post } = await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await sendTurn()
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_new1'))
    act(() => live.nav.setConversationId(null))
    await sendTurn('A new line of questioning entirely')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls.map((c) => c[2])).toEqual(['cnv_new1', 'cnv_new2'])
    expect(server.posts().map((c) => c.url)).toEqual(['/conversations', '/conversations'])
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_new2'))
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

describe('a campaign switch scopes the pane (I-13, brief section 14; review pr212 H-1, H-2)', () => {
  it("drops A's failed first turn from the pane, and B's first send makes and posts to a thread of B's own (P3)", async () => {
    const { server, post } = await mount({ turns: [{ kind: 'error', message: 'The oracle is silent.' }] })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await sendTurn()
    expect(await screen.findByText('The oracle is silent.')).toBeInTheDocument()
    expect(live.nav.conversationId).toBeNull()
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_B')) })
    expect(live.c.selection).toMatchObject({ kind: 'selected', campaign: { campaign_id: 'cmp_B' } })
    expect(screen.queryByText(PROMPT)).toBeNull()
    expect(screen.queryByText('The oracle is silent.')).toBeNull()
    await sendTurn('A question for the second campaign')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls.map((c) => c[2])).toEqual(['cnv_new1', 'cnv_new2'])
    expect(server.posts().map((c) => (c.body as { campaign_id: string }).campaign_id)).toEqual(['cmp_A', 'cmp_B'])
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_new2'))
  })

  it("drops A's first turn still in flight: never drawn under B, never adopted, and B's send is B's own (P4)", async () => {
    const { post } = await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    let answer: (result: ChatResult) => void = () => {}
    post.mockImplementationOnce(() => new Promise<ChatResult>((resolve) => { answer = resolve }))
    await sendTurn()
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][2]).toBe('cnv_new1')
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_B')) })
    expect(screen.queryByText(PROMPT)).toBeNull()
    await act(async () => {
      answer({ kind: 'ok', response: { answer: 'Under the lighthouse stair.', sources: [], answerable: true, conversation_id: 'cnv_new1' } })
    })
    expect(screen.queryByText(PROMPT)).toBeNull()
    expect(screen.queryByText('Under the lighthouse stair.')).toBeNull()
    expect(live.stored.conversationId).toBeNull()
    expect(live.nav.conversationId).toBeNull()
    await sendTurn('A question for the second campaign')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls[1][2]).toBe('cnv_new2')
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_new2'))
  })

  it("a pick from the loaded list, with no re-read, drops A's failed first turn (P6)", async () => {
    const { server } = await mount({ turns: [{ kind: 'error', message: 'The oracle is silent.' }] })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => live.c.loadCampaigns())
    await waitFor(() => expect(live.c.list.kind).toBe('ready'))
    await sendTurn()
    expect(await screen.findByText('The oracle is silent.')).toBeInTheDocument()
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_B')) })
    expect(live.c.selection).toMatchObject({ kind: 'selected', campaign: { campaign_id: 'cmp_B' } })
    expect(server.calls.filter((c) => c.url === '/campaigns/cmp_B')).toEqual([])
    expect(screen.queryByText(PROMPT)).toBeNull()
    expect(screen.queryByText('The oracle is silent.')).toBeNull()
  })

  it("a pick from the loaded list drops A's first turn still in flight: never drawn under B, never adopted (P7)", async () => {
    const { server, post } = await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => live.c.loadCampaigns())
    await waitFor(() => expect(live.c.list.kind).toBe('ready'))
    let answer: (result: ChatResult) => void = () => {}
    post.mockImplementationOnce(() => new Promise<ChatResult>((resolve) => { answer = resolve }))
    await sendTurn()
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_B')) })
    expect(server.calls.filter((c) => c.url === '/campaigns/cmp_B')).toEqual([])
    expect(screen.queryByText(PROMPT)).toBeNull()
    await act(async () => {
      answer({ kind: 'ok', response: { answer: 'Under the lighthouse stair.', sources: [], answerable: true, conversation_id: 'cnv_new1' } })
    })
    expect(screen.queryByText(PROMPT)).toBeNull()
    expect(screen.queryByText('Under the lighthouse stair.')).toBeNull()
    expect(live.stored.conversationId).toBeNull()
  })

  it("a switch to a campaign that is not available drops A's failed first turn too (P10)", async () => {
    await mount({ turns: [{ kind: 'error', message: 'The oracle is silent.' }] })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await sendTurn()
    expect(await screen.findByText('The oracle is silent.')).toBeInTheDocument()
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_Gone')) })
    expect(live.c.selection.kind).toBe('unavailable')
    expect(screen.queryByText(PROMPT)).toBeNull()
    expect(screen.queryByText('The oracle is silent.')).toBeNull()
  })
})

describe('the pane keeps its turns where no switch happened (review pr212 M-1, M-3, L-1)', () => {
  it.each([403, 404])('a thread create refused with %i mid-send keeps the prompt, says why and announces it; a clear starts afresh (P5)', async (status) => {
    const { post } = await mount({ create: { status } })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await sendTurn()
    await waitFor(() => expect(live.c.selection.kind).toBe('unavailable'))
    expect(await screen.findByText(NOT_STARTED)).toBeInTheDocument()
    expect(screen.getByText(PROMPT)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Answer failed')
    expect(post).not.toHaveBeenCalled()
    await act(async () => { await live.c.clearCampaign() })
    expect(screen.queryByText(PROMPT)).toBeNull()
  })

  it('leaving GM while a campaign turn is in flight keeps the turn on screen until it settles, then crosses without it (P8)', async () => {
    const { post } = await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    let answer: (result: ChatResult) => void = () => {}
    post.mockImplementationOnce(() => new Promise<ChatResult>((resolve) => { answer = resolve }))
    await sendTurn()
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    act(() => live.nav.setMode('sage'))
    expect(screen.getByText(PROMPT)).toBeInTheDocument()
    await act(async () => {
      answer({ kind: 'ok', response: { answer: 'Under the lighthouse stair.', sources: [], answerable: true, conversation_id: 'cnv_new1' } })
    })
    expect(screen.queryByText(PROMPT)).toBeNull()
    expect(live.stored.conversationId).toBeNull()
  })

  it('a campaign switch on the Sage side keeps the Sage pane (P9)', async () => {
    const { post } = await mount()
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => live.nav.setMode('sage'))
    await sendTurn('A Sage question before the switch')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('A Sage question before the switch')).toBeInTheDocument()
    await act(async () => { await live.c.selectCampaign(campaignBody('cmp_B')) })
    expect(live.c.selection).toMatchObject({ kind: 'selected', campaign: { campaign_id: 'cmp_B' } })
    expect(screen.getByText('A Sage question before the switch')).toBeInTheDocument()
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
