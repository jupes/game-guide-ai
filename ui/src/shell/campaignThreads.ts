/**
 * campaignThreads -- a campaign's GM threads (agent-forge-harness-1kg.2.5, PR-2;
 * brief sections 7.1 and 7.7 and I-12, as amended by its Critic's items 8 and 17).
 *
 * Server-only and memory-only (RAIL-26, SEC-49): listed from the server, titled
 * by the server or neutrally, never written to web storage, created lazily by
 * the first send -- with no title -- and renamed with `PATCH`.
 *
 * The state lives in the campaign provider (critic 8), so LeftNav and ChatPane
 * share it and it survives ChatPane's remount across the GM boundary. A list is
 * stored under the `scope.key` it was requested under and is READ only while
 * that key is the current scope: a switch never draws the previous campaign's
 * rows for even one render (LIB-25), and no effect has to clear anything.
 */

import * as React from 'react'
import type { Conversation } from '../gm/contracts'
import { createCampaignThread, listCampaignThreads, renameThread } from './campaignApi'
import type { CampaignScope } from './campaignContext'

export interface ThreadRow {
  readonly id: string
  /** What to show: the server's title, or `GM thread · <date>`. */
  readonly title: string
  readonly createdAt: string
}

export type RenameOutcome = 'renamed' | 'invalid' | 'failed' | 'gone'

export interface CampaignThreadsValue {
  readonly threads: readonly ThreadRow[]
  /** `loading` while any page is read; `failed` until `retry()` asks again. */
  readonly status: 'idle' | 'loading' | 'ready' | 'failed'
  readonly hasMore: boolean
  loadMore(): void
  retry(): void
  rename(id: string, title: string): Promise<RenameOutcome>
  /** The thread the next send posts to: this scope's created-but-unopened one,
   * or a new one (single-flight per scope). `null` when none could be made. */
  ensureThread(): Promise<string | null>
}

/** I-18: replaceable copy. */
export const GM_THREAD_TITLE = 'GM thread'

function toRow(conversation: Conversation): ThreadRow {
  const date = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(conversation.created_at))
  return {
    id: conversation.conversation_id,
    title: conversation.title ?? `${GM_THREAD_TITLE} · ${date}`,
    createdAt: conversation.created_at,
  }
}

interface ThreadList {
  readonly rows: readonly ThreadRow[]
  readonly nextCursor: string | null
  /** The read in flight, if any. */
  readonly reading: 'first' | 'more' | null
  /** The read that failed last: what `retry` asks for again. */
  readonly failed: 'first' | 'more' | null
}

/** What the campaign provider lends the thread store. */
export interface ThreadHost {
  /** A fetch aborted by an identity change or an unmount. */
  fetcher(): typeof fetch
  isCurrentScope(key: string): boolean
  /** These ids are threads of scope `key` (listed or created). */
  claim(key: string, ids: readonly string[]): void
  /** A create's 403 or 404: the scope's campaign is not there any more. */
  unavailable(key: string): void
}

export class ThreadStore {
  private lists: ReadonlyMap<string, ThreadList> = new Map()
  private readonly listeners = new Set<() => void>()
  private readonly creating = new Map<string, Promise<string | null>>()
  /** Made by a send and not opened yet: the next send of its scope reuses it. */
  private fresh: { readonly key: string; readonly id: string } | null = null
  private readonly host: ThreadHost

  constructor(host: ThreadHost) {
    this.host = host
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getSnapshot = (): ReadonlyMap<string, ThreadList> => this.lists

  private put(key: string, list: ThreadList): void {
    const next = new Map(this.lists)
    for (const old of next.keys()) if (!this.host.isCurrentScope(old)) next.delete(old)
    next.set(key, list)
    this.lists = next
    for (const listener of [...this.listeners]) listener()
  }

  /** An identity change: nothing of the previous account survives. */
  reset(): void {
    this.lists = new Map()
    this.creating.clear()
    this.fresh = null
    for (const listener of [...this.listeners]) listener()
  }

  /** The nav opened `id`: a fresh thread is fresh no longer. */
  opened(id: string | null): void {
    if (this.fresh?.id === id) this.fresh = null
  }

  load(scope: CampaignScope): void {
    if (!this.lists.has(scope.key)) this.read(scope, 'first')
  }

  loadMore(scope: CampaignScope): void {
    const list = this.lists.get(scope.key)
    if (list !== undefined && list.nextCursor !== null && list.reading === null && list.failed === null) {
      this.read(scope, 'more')
    }
  }

  retry(scope: CampaignScope): void {
    const list = this.lists.get(scope.key)
    if (list !== undefined && list.failed !== null && list.reading === null) this.read(scope, list.failed)
  }

  private read({ key, campaignId }: CampaignScope, which: 'first' | 'more'): void {
    const before = this.lists.get(key) ?? { rows: [], nextCursor: null, reading: null, failed: null }
    this.put(key, { ...before, reading: which, failed: null })
    const cursor = which === 'more' ? before.nextCursor : null
    void listCampaignThreads(campaignId, cursor, this.host.fetcher()).then((result) => {
      const now = this.lists.get(key)
      if (now === undefined || now.reading !== which || !this.host.isCurrentScope(key)) return
      if (result.kind !== 'ok') {
        this.put(key, { ...now, reading: null, failed: which })
        return
      }
      const page = result.items.map(toRow)
      this.host.claim(key, page.map((row) => row.id))
      const shown = new Set(now.rows.map((row) => row.id))
      const listed = new Set(page.map((row) => row.id))
      const rows = which === 'first'
        ? [...now.rows.filter((row) => !listed.has(row.id)), ...page]
        : [...now.rows, ...page.filter((row) => !shown.has(row.id))]
      this.put(key, { rows, nextCursor: result.nextCursor, reading: null, failed: null })
    })
  }

  async rename(scope: CampaignScope, id: string, title: string): Promise<RenameOutcome> {
    const result = await renameThread(id, title, this.host.fetcher())
    if (result.kind === 'unauthorized') return 'failed'
    if (result.kind === 'invalid' || result.kind === 'failed') return result.kind
    const list = this.lists.get(scope.key)
    if (list !== undefined && this.host.isCurrentScope(scope.key)) {
      const rows = result.kind === 'gone'
        ? list.rows.filter((row) => row.id !== id)
        : list.rows.map((row) => (row.id === id ? toRow(result.conversation) : row))
      this.put(scope.key, { ...list, rows })
    }
    return result.kind
  }

  ensureThread({ key, campaignId }: CampaignScope): Promise<string | null> {
    if (this.fresh?.key === key) return Promise.resolve(this.fresh.id)
    const inFlight = this.creating.get(key)
    if (inFlight !== undefined) return inFlight
    const run = createCampaignThread(campaignId, this.host.fetcher()).then((result) => {
      this.creating.delete(key)
      if (!this.host.isCurrentScope(key)) return null
      if (result.kind === 'unavailable') this.host.unavailable(key)
      if (result.kind !== 'created') return null
      const row = toRow(result.conversation)
      this.host.claim(key, [row.id])
      this.fresh = { key, id: row.id }
      // At once at the top of a list already shown; an unread list gets it from the server.
      const list = this.lists.get(key)
      if (list !== undefined) this.put(key, { ...list, rows: [row, ...list.rows.filter((r) => r.id !== row.id)] })
      return row.id
    })
    this.creating.set(key, run)
    return run
  }
}

interface ThreadsContextValue {
  readonly store: ThreadStore
  readonly scope: CampaignScope | null
}

/** Provided by `CampaignProvider` with the scope of the same render. */
export const CampaignThreadsContext = React.createContext<ThreadsContextValue | null>(null)

const NO_LISTS: ReadonlyMap<string, ThreadList> = new Map()
const noSubscription = (): (() => void) => () => {}
const noLists = (): ReadonlyMap<string, ThreadList> => NO_LISTS
const NO_THREADS: CampaignThreadsValue = {
  threads: [],
  status: 'idle',
  hasMore: false,
  loadMore: () => {},
  retry: () => {},
  rename: () => Promise.resolve('failed'),
  ensureThread: () => Promise.resolve(null),
}

/** The current scope's GM threads. Mounting a consumer while a campaign is
 * selected reads the first page; outside a provider, or with no campaign
 * selected, an inert value that never fetches. */
export function useCampaignThreads(): CampaignThreadsValue {
  const context = React.useContext(CampaignThreadsContext)
  const store = context?.store ?? null
  const scope = context?.scope ?? null
  const lists = React.useSyncExternalStore(store?.subscribe ?? noSubscription, store?.getSnapshot ?? noLists)
  React.useEffect(() => {
    if (store !== null && scope !== null) store.load(scope)
  }, [store, scope])
  return React.useMemo<CampaignThreadsValue>(() => {
    if (store === null || scope === null) return NO_THREADS
    const list = lists.get(scope.key)
    return {
      threads: list?.rows ?? [],
      status: list === undefined ? 'idle' : list.reading !== null ? 'loading' : list.failed !== null ? 'failed' : 'ready',
      hasMore: list !== undefined && list.nextCursor !== null,
      loadMore: () => store.loadMore(scope),
      retry: () => store.retry(scope),
      rename: (id, title) => store.rename(scope, id, title),
      ensureThread: () => store.ensureThread(scope),
    }
  }, [store, scope, lists])
}
