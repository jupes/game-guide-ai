import * as React from 'react'
import type { ChatMode } from '../api'
import type { Conversation as ServerConversation } from '../gm/contracts'
import { bindConversationChannel, listOwnConversations } from './campaignApi'
import { canUseCampaigns } from './campaignContext'
import type { ConversationStore } from './conversationStore'
import { CurrentUserContext } from './currentUser'

type Rows = ReadonlyMap<string, ServerConversation>
const EMPTY: Rows = new Map()
const MAX_PAGES = 10
const isLegacy = (row: ServerConversation): boolean => row.campaign_id === null && row.archived_at === null

async function readIndex(fetchImpl: typeof fetch, isCurrent: () => boolean): Promise<Rows> {
  const rows = new Map<string, ServerConversation>()
  let cursor: string | null = null
  for (let i = 0; i < MAX_PAGES; i += 1) {
    const page = await listOwnConversations(cursor, fetchImpl)
    if (page === null || !isCurrent()) return EMPTY
    for (const item of page.items) {
      if (item.kind === 'ok' && isLegacy(item.value)) rows.set(item.value.conversation_id, item.value)
    }
    cursor = page.next_cursor
    if (cursor === null) break
  }
  return rows
}

/** Lifetime of one sign-in, shared by StrictMode's effect replay. */
function createSession(account: string | null, enabled: boolean, fetchImpl: typeof fetch) {
  let current = false
  let read: Promise<Rows> | undefined
  let known: Rows = EMPTY
  const attempted = new Set<string>()
  return {
    isCurrent: () => current,
    setCurrent: (value: boolean) => { current = value && account !== null },
    setRows: (rows: Rows) => { known = rows },
    read: () => (read ??= enabled ? readIndex(fetchImpl, () => current) : Promise.resolve(EMPTY)),
    bind: async (id: string, mode: ChatMode) => {
      if (!current || !enabled || attempted.has(id) || known.get(id)?.started_mode === mode) return null
      attempted.add(id)
      const row = await bindConversationChannel(id, mode, fetchImpl)
      return current && row !== null && isLegacy(row) ? row : null
    },
  }
}

type Session = ReturnType<typeof createSession>
interface IndexValue {
  rows: Rows
  isCurrent(): boolean
  bindAfterTurn(id: string, mode: ChatMode): void
}

const IndexContext = React.createContext<IndexValue>({ rows: EMPTY, isCurrent: () => true, bindAfterTurn: () => {} })

export function ConversationIndexProvider({ children, fetchImpl = fetch }: {
  children: React.ReactNode
  fetchImpl?: typeof fetch
}): React.JSX.Element {
  const user = React.useContext(CurrentUserContext)
  const account = user?.authStatus === 'authenticated' ? user.user.id : null
  const enabled = account !== null && canUseCampaigns(user?.user.role ?? 'player')
  const session = React.useMemo(() => createSession(account, enabled, fetchImpl), [account, enabled, fetchImpl])
  const [loaded, setLoaded] = React.useState<{ session: Session; rows: Rows } | null>(null)
  const [bound, setBound] = React.useState<{ session: Session; rows: Rows } | null>(null)

  React.useLayoutEffect(() => {
    session.setCurrent(true)
    return () => session.setCurrent(false)
  }, [session])

  React.useEffect(() => {
    if (!enabled) return
    let live = true
    void session.read().then((rows) => {
      if (live) setLoaded({ session, rows })
    })
    return () => { live = false }
  }, [session, enabled])

  const rows = React.useMemo(() => {
    const index = loaded?.session === session ? loaded.rows : EMPTY
    return bound?.session === session ? new Map([...index, ...bound.rows]) : index
  }, [loaded, bound, session])
  React.useLayoutEffect(() => session.setRows(rows), [rows, session])

  const value = React.useMemo<IndexValue>(() => ({
    rows,
    isCurrent: session.isCurrent,
    bindAfterTurn(id, mode) {
      void session.bind(id, mode).then((row) => {
        if (row === null || !session.isCurrent()) return
        setBound((previous) => ({ session, rows: new Map([
          ...(previous?.session === session ? previous.rows : EMPTY), [id, row],
        ]) }))
      })
    },
  }), [rows, session])

  return <IndexContext.Provider value={value}>{children}</IndexContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useConversationIndex(): IndexValue {
  return React.useContext(IndexContext)
}

export interface IndexRow {
  id: string
  title: string
  createdAt: string
}

// eslint-disable-next-line react-refresh/only-export-components -- shared selector; HMR-only rule
export const newestFirst = (a: IndexRow, b: IndexRow): number => Date.parse(b.createdAt) - Date.parse(a.createdAt)

/** Server-only rows supplement the local list, even when the server has a
 * different channel or title for a row this browser already knows. */
// eslint-disable-next-line react-refresh/only-export-components -- shared selector; HMR-only rule
export function serverOnlyRows(rows: Rows, store: ConversationStore, mode: ChatMode | null): IndexRow[] {
  return [...rows.values()]
    .filter((row) => row.started_mode === mode && store.get(row.conversation_id) === undefined)
    .map((row) => ({
      id: row.conversation_id,
      title: row.title ?? `Conversation · ${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(row.created_at))}`,
      createdAt: row.created_at,
    }))
    .sort(newestFirst)
}

// eslint-disable-next-line react-refresh/only-export-components -- shared selector; HMR-only rule
export function conversationInChannel(
  id: string | null, mode: ChatMode, store: ConversationStore, rows: Rows,
): string | null {
  if (id === null) return null
  const known = store.get(id)?.mode ?? rows.get(id)?.started_mode
  return known == null || known === mode ? id : null
}
