import { StrictMode } from 'react'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Conversation as ServerConversation } from '../gm/contracts'
import { AppNavProvider } from './AppNav'
import { ChatPane } from './ChatPane'
import { ConversationIndexProvider } from './conversationIndex'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { conversationsStorageKey, LocalStorageConversationStore, MemoryConversationStore, type ConversationStore } from './conversationStore'
import { CurrentUserContext, STUB, type CurrentUserContextValue, type UserRole } from './currentUser'
import { LeftNav } from './LeftNav'

const wire = (id: string, overrides: Partial<ServerConversation> = {}): ServerConversation => ({
  schema_version: 1, conversation_id: id, campaign_id: null, title: null, started_mode: null,
  created_at: '2026-10-02T00:00:00Z', updated_at: null, archived_at: null, ...overrides,
})
const page = (items: unknown[], cursor: string | null = null): Response =>
  Response.json({ schema_version: 1, items, next_cursor: cursor })
const nav = () => screen.getByRole('navigation', { name: 'Main navigation' })
const button = (name: string) => within(nav()).getByRole('button', { name })

function user(id: string, role: UserRole = 'dm'): CurrentUserContextValue {
  return {
    user: { ...STUB, id, role }, authStatus: 'authenticated',
    retryAuthCheck: () => {}, signIn: () => {}, setDisplayName: () => {}, setAvatarTone: () => {},
  }
}

function IndexApp({ identity, store, fetchImpl, withChat = false }: {
  identity: CurrentUserContextValue; store: ConversationStore; fetchImpl: typeof fetch; withChat?: boolean
}) {
  return <StrictMode><CurrentUserContext.Provider value={identity}>
    <AppNavProvider><ConversationStoreProvider store={store}>
      <ConversationIndexProvider fetchImpl={fetchImpl}>
        <LeftNav />
        {withChat && <ChatPane />}
      </ConversationIndexProvider>
    </ConversationStoreProvider></AppNavProvider>
  </CurrentUserContext.Provider></StrictMode>
}

afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals() })

describe('DM conversation index', () => {
  it('merges server-only rows in memory, preserves local metadata, and fields no campaign or archived rows', async () => {
    const store = new LocalStorageConversationStore('ada')
    const local = store.create('sage', 'Local title')
    const key = conversationsStorageKey('ada')
    const before = localStorage.getItem(key)
    const read = [
      wire(local.id, { started_mode: null, title: null }),
      wire('cnv_older', { started_mode: 'sage', title: 'Older server title', created_at: '2026-10-01T00:00:00Z' }),
      wire('cnv_newer', { started_mode: 'sage', title: 'Newer server title' }),
      wire('cnv_rules', { started_mode: 'rules', title: 'Known Rules' }),
      wire('cnv_other', { title: 'Unsorted conversation' }),
      wire('cnv_fallback'),
      wire('cnv_campaign', { campaign_id: 'cmp_A', title: 'Campaign title' }),
      wire('cnv_archived', { archived_at: '2026-10-03T00:00:00Z', title: 'Archived title' }),
    ]
    const fetchImpl = vi.fn(async () => page(read))
    const first = render(<IndexApp identity={user('ada')} store={store} fetchImpl={fetchImpl} />)
    await screen.findByRole('button', { name: 'Unsorted conversation' })
    const visible = [...nav().querySelectorAll('.left-nav__conversation')].map((node) => node.textContent)
    expect(visible.slice(0, 3)).toEqual(['Local title', 'Newer server title', 'Older server title'])
    const other = screen.getByRole('group', { name: 'Other conversations' })
    expect(within(other).getByRole('button', { name: /^Conversation · / })).toBeVisible()
    expect(within(other).getAllByRole('button')).toHaveLength(2)
    expect(screen.queryByText('Campaign title')).toBeNull()
    expect(screen.queryByText('Archived title')).toBeNull()
    expect(localStorage.getItem(key)).toBe(before)
    expect(fetchImpl.mock.calls).toHaveLength(1)
    const markup = nav().innerHTML.replace(/_r_[^_]*_/g, 'generated')
    first.unmount()
    render(<IndexApp identity={user('ada')} store={store} fetchImpl={fetchImpl} />)
    await screen.findByRole('button', { name: 'Unsorted conversation' })
    expect(nav().innerHTML.replace(/_r_[^_]*_/g, 'generated')).toBe(markup)
    await userEvent.click(button('Rules'))
    expect(button('Known Rules')).toHaveAttribute('aria-pressed', 'true')
    expect(button('Unsorted conversation')).toBeVisible()
    expect(screen.queryByText('Local title')).toBeNull()
    expect(localStorage.getItem(key)).toBe(before)
    expect(fetchImpl.mock.calls).toHaveLength(2)
  })

  it.each([404, 405, 503, 'network', 'invalid JSON'] as const)('leaves the local list alone on %s', async (failure) => {
    const store = new MemoryConversationStore()
    store.create('sage', 'Offline local title')
    const fetchImpl = vi.fn(async () => {
      if (failure === 'network') throw new TypeError('offline')
      if (failure === 'invalid JSON') return new Response('<html>')
      return new Response(null, { status: failure })
    })
    render(<IndexApp identity={user('ada')} store={store} fetchImpl={fetchImpl} />)
    const before = nav().innerHTML
    await act(async () => {})
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(nav().innerHTML).toBe(before)
    expect(button('Offline local title')).toBeVisible()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('never reads the index for a player, even when rendering the drawer', async () => {
    const store = new MemoryConversationStore()
    store.create('sage', 'Player local title')
    const fetchImpl = vi.fn(async () => page([wire('cnv_hidden', { title: 'Server title' })]))
    render(<IndexApp identity={user('bob', 'player')} store={store} fetchImpl={fetchImpl} />)
    await act(async () => {})
    expect(button('Player local title')).toBeVisible()
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('caps the account read at ten pages, even while the server keeps returning cursors', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => page([wire('cnv_cap', { title: 'Returned row' })], 'more_opaque'))
    render(<IndexApp identity={user('ada')} store={new MemoryConversationStore()} fetchImpl={fetchImpl} />)
    await screen.findByRole('button', { name: 'Returned row' })
    expect(fetchImpl.mock.calls.map(([path]) => path)).toEqual([
      '/conversations', ...Array<string>(9).fill('/conversations?cursor=more_opaque'),
    ])
  })

  it('drops a previous account’s deferred page and cursor before it can render or start another request', async () => {
    let first!: (response: Response) => void
    const held = new Promise<Response>((resolve) => { first = resolve })
    const store = new MemoryConversationStore()
    const fetchImpl = vi.fn<typeof fetch>()
      .mockImplementationOnce(async () => held)
      .mockImplementation(async () => page([wire('cnv_bob', { title: 'New account row' })]))
    const app = render(<IndexApp identity={user('ada')} store={store} fetchImpl={fetchImpl} />)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    app.rerender(<IndexApp identity={user('bob')} store={store} fetchImpl={fetchImpl} />)
    await screen.findByRole('button', { name: 'New account row' })
    const observed: string[] = []
    const observer = new MutationObserver(() => { observed.push(nav().textContent ?? '') })
    observer.observe(nav(), { childList: true, subtree: true, characterData: true })
    await act(async () => { first(page([wire('cnv_ada', { title: 'Old account row' })], 'old-cursor')) })
    observer.disconnect()
    expect(screen.queryByText('Old account row')).toBeNull()
    expect(observed.join('')).not.toContain('Old account row')
    expect(button('New account row')).toBeVisible()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    app.rerender(<IndexApp identity={user('ada')} store={store} fetchImpl={fetchImpl} />)
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(3))
  })

  it.each([200, 409])('binds an unclassified row once, after a successful turn (PATCH %s)', async (status) => {
    const calls: Array<[string, string, string | null]> = []
    const unknown = wire('cnv_other', { title: 'An unclassified thread' })
    const fetchImpl = vi.fn<typeof fetch>(async (request, init) => {
      const path = String(request)
      calls.push([init?.method ?? 'GET', path, typeof init?.body === 'string' ? init.body : null])
      if (path === '/conversations') return page([unknown])
      if (path === '/chat') return Response.json({ answer: 'Spell result', answerable: true, sources: [], conversation_id: unknown.conversation_id })
      if (init?.method === 'PATCH') return Response.json({ ...unknown, started_mode: 'spell' }, { status })
      const id = path.split('/')[2]
      if (path.endsWith('/attachments')) return Response.json({ conversation_id: id, attachments: [] })
      return Response.json({ conversation_id: id, messages: [] })
    })
    vi.stubGlobal('fetch', fetchImpl)
    const action = userEvent.setup()
    render(<IndexApp identity={user('ada')} store={new MemoryConversationStore()} fetchImpl={fetchImpl} withChat />)
    await screen.findByRole('group', { name: 'Other conversations' })
    await action.click(button('Spell'))
    await action.click(button('An unclassified thread'))
    for (const question of ['First question', 'Second question', 'Third question']) {
      await action.type(screen.getByPlaceholderText('Ask…'), question)
      await action.click(screen.getByRole('button', { name: 'Send message' }))
      await screen.findByText(question)
    }
    const writes = calls.filter(([method]) => method === 'POST' || method === 'PATCH')
    expect(writes.map(([method, path]) => `${method} ${path}`)).toEqual([
      'POST /chat', 'PATCH /conversations/cnv_other', 'POST /chat', 'POST /chat',
    ])
    expect(JSON.parse(writes[1][2] ?? '')).toEqual({ schema_version: 1, started_mode: 'spell' })
    expect(JSON.parse(writes[0][2] ?? '')).toMatchObject({ mode: 'spell', conversation_id: 'cnv_other' })
    expect(screen.getAllByText('Spell result')).toHaveLength(3)
    expect(button('An unclassified thread')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('group', { name: 'Other conversations' }) !== null).toBe(status !== 200)
  })
})
