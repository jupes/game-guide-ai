import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppRoot } from '../AppRoot'
import type { ChatMode, ChatResult } from '../api'
import { installMatchMediaWidth, type MatchMediaWidthStub } from '../testing/matchMediaWidth'
import { AppNavProvider, useAppNav } from './AppNav'
import { ChatPane } from './ChatPane'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { conversationsStorageKey, LocalStorageConversationStore, MemoryConversationStore } from './conversationStore'

const PLAYER = 'channel-player@example.test'
const USER = { email: PLAYER, role: 'player' }
const emptyHistory = async () => ({ kind: 'ok' as const, messages: [] })
type Turn = { prompt: string; mode: ChatMode; conversation_id: string | null }

function boot(path: '/' | '/workspace', initialPost?: Promise<Response>) {
  window.history.replaceState(null, '', path)
  const calls: string[] = []
  const turns: Turn[] = []
  vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (request, init) => {
    const url = String(request)
    calls.push(`${init?.method ?? 'GET'} ${url}`)
    if (url === '/auth/me') return Response.json(USER)
    if (url === '/chat') {
      const turn = JSON.parse(String(init?.body)) as Turn
      turns.push(turn)
      if (turns.length === 1 && initialPost !== undefined) return initialPost
      return answer(turn.mode, turn.conversation_id ?? `cnv_new_${turn.mode}`)
    }
    const id = url.split('/')[2]
    if (url.endsWith('/messages')) return Response.json({ conversation_id: id, messages: [] })
    if (url.endsWith('/attachments')) return Response.json({ conversation_id: id, attachments: [] })
    return Response.json({}, { status: 404 })
  }))
  render(<AppRoot />)
  return { calls, turns }
}

function answer(mode: ChatMode, id: string): Response {
  return Response.json({ answer: `Answer in ${mode}`, answerable: true, sources: [], conversation_id: id })
}

async function send(user: ReturnType<typeof userEvent.setup>, prompt: string): Promise<void> {
  await user.type(screen.getByPlaceholderText('Ask…'), prompt)
  await user.click(screen.getByRole('button', { name: 'Send message' }))
}

async function switchFromDrawer(user: ReturnType<typeof userEvent.setup>, name: string): Promise<void> {
  await user.click(await screen.findByRole('button', { name: 'Open navigation' }))
  const drawer = screen.getByRole('dialog', { name: 'Navigation' })
  await user.click(within(drawer).getByRole('button', { name }))
  expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull()
}

describe('channel selection and sends (I-15)', () => {
  let viewport: MatchMediaWidthStub
  beforeEach(() => { viewport = installMatchMediaWidth(375) })
  afterEach(() => {
    cleanup()
    localStorage.clear()
    viewport.restore()
    vi.unstubAllGlobals()
    window.history.replaceState(null, '', '/')
  })

  it('leaves Sage history behind and adopts the new Spell conversation from the phone drawer', async () => {
    const user = userEvent.setup()
    const store = new LocalStorageConversationStore(PLAYER)
    const sage = store.create('sage', 'Earlier Sage question')
    const { calls, turns } = boot('/')
    await user.click(await screen.findByRole('button', { name: 'Sage' }))
    await send(user, 'Ask Sage')
    expect(await screen.findByText('Answer in sage')).toBeVisible()
    expect(turns[0]?.conversation_id).toBe(sage.id)
    const count = calls.length

    await switchFromDrawer(user, 'Spell')
    expect(screen.queryByText('Answer in sage')).toBeNull()
    expect(store.list('spell')).toEqual([])
    await send(user, 'Ask Spell')
    expect(await screen.findByText('Answer in spell')).toBeVisible()
    expect(turns).toMatchObject([{ mode: 'sage', conversation_id: sage.id }, { mode: 'spell', conversation_id: null }])
    expect(calls.slice(count).some((line) => line.includes(sage.id))).toBe(false)
    expect(calls.filter((line) => /^(GET|PATCH) \/conversations(?:\?|$|\/[^/]+$)/.test(line))).toEqual([])
    expect(new LocalStorageConversationStore(PLAYER).list('spell')).toMatchObject([
      { id: 'cnv_new_spell', mode: 'spell', title: 'Ask Spell', boundPreference: 'auto' },
    ])
    await switchFromDrawer(user, 'Sage')
    expect(screen.queryByText('Answer in spell')).toBeNull()
    expect(calls).toContain(`GET /conversations/${sage.id}/messages`)
  })

  it('selects the latest destination row from both Landing and the drawer', async () => {
    const user = userEvent.setup()
    const store = new LocalStorageConversationStore(PLAYER)
    const older = store.create('spell', 'Older Spell question')
    const newer = store.create('spell', 'Latest Spell question')
    // Use explicit dates so clock resolution cannot decide which one wins.
    const key = conversationsStorageKey(PLAYER)
    const stored = localStorage.getItem(key)
    expect(stored).not.toBeNull()
    localStorage.setItem(key, JSON.stringify([
      { ...newer, createdAt: '2026-10-02T00:00:00Z' }, { ...older, createdAt: '2026-10-01T00:00:00Z' },
    ]))
    const sage = store.create('sage', 'Sage question')
    const { turns } = boot('/')
    await user.click(await screen.findByRole('button', { name: 'Sage' }))
    await send(user, 'A Sage turn')
    expect(await screen.findByText('Answer in sage')).toBeVisible()
    await switchFromDrawer(user, 'Spell')
    await send(user, 'A Spell turn')
    expect(await screen.findByText('Answer in spell')).toBeVisible()
    expect(turns.map((turn) => turn.conversation_id)).toEqual([sage.id, newer.id])
    cleanup()
    const landing = boot('/')
    await user.click(await screen.findByRole('button', { name: 'Spell' }))
    await send(user, 'Return from Landing')
    expect(landing.turns[0]?.conversation_id).toBe(newer.id)
    const header = screen.getByRole('navigation', { name: 'Channels' })
    await user.click(within(header).getByRole('button', { name: 'Sage' }))
    await send(user, 'Return from the channel bar')
    expect(landing.turns[1]?.conversation_id).toBe(sage.id)
  })

  it.each([false, true])('keeps a late Sage answer out of Spell (saved thread: %s)', async (savedThread) => {
    let finish!: (response: Response) => void
    const user = userEvent.setup()
    const store = new LocalStorageConversationStore(PLAYER)
    const spell = savedThread ? store.create('spell', 'Saved Spell question') : undefined
    const { calls, turns } = boot('/', new Promise((resolve) => { finish = resolve }))
    await user.click(await screen.findByRole('button', { name: 'Sage' }))
    await send(user, 'Unfinished Sage')
    expect(turns).toHaveLength(1)
    await switchFromDrawer(user, 'Spell')
    expect(screen.queryByText('Unfinished Sage')).toBeNull()
    await act(async () => { finish(answer('sage', 'cnv_late_sage')) })
    expect(screen.queryByText('Answer in sage')).toBeNull()
    expect(calls.some((line) => line.includes('cnv_late_sage'))).toBe(false)
    expect(store.get('cnv_late_sage')?.mode).toBe('sage')
    await send(user, 'Independent Spell')
    expect(await screen.findByText('Answer in spell')).toBeVisible()
    expect(turns[1]).toMatchObject({ mode: 'spell', conversation_id: spell?.id ?? null })
  })

  it('uses a fresh id even when a caller changes mode without the channel switch helper', async () => {
    const store = new MemoryConversationStore()
    const rules = store.create('rules', 'Pinned Rules')
    const post = vi.fn(async (): Promise<ChatResult> => ({ kind: 'ok', response: {
      answer: 'A separate Spell answer', answerable: true, sources: [], conversation_id: 'cnv_isolated',
    } }))
    const loadHistory = vi.fn(emptyHistory)
    function Probe() {
      const { setConversationId, setMode } = useAppNav()
      return <>
        <button onClick={() => setConversationId(rules.id)}>Open Rules directly</button>
        <button onClick={() => setMode('spell')}>Force Spell directly</button>
        <ChatPane post={post} loadHistory={loadHistory} />
      </>
    }
    const user = userEvent.setup()
    render(<AppNavProvider initialMode="rules"><ConversationStoreProvider store={store}>
      <Probe />
    </ConversationStoreProvider></AppNavProvider>)
    await user.click(screen.getByText('Open Rules directly'))
    await waitFor(() => expect(loadHistory).toHaveBeenCalledWith(rules.id))
    loadHistory.mockClear()
    await user.click(screen.getByText('Force Spell directly'))
    await send(user, 'Backstop test')
    expect(post).toHaveBeenCalledWith('Backstop test', 'spell', null, 'auto')
    expect(await screen.findByText('A separate Spell answer')).toBeVisible()
    expect(loadHistory).not.toHaveBeenCalledWith(rules.id)
    expect(store.get('cnv_isolated')?.mode).toBe('spell')
  })
})
