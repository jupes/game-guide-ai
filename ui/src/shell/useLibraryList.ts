/**
 * useLibraryList -- one category of the Campaign Library, read from the server
 * (agent-forge-harness-1kg.6.4; brief 2.3; LIB-20 to LIB-25, STATE-1, X-7).
 *
 * - **One request per key.** The key is the scope key, category, filter, sort, type,
 *   settled search, `documentsVersion` and the retry count. The state it renders is the
 *   loaded one only while `loaded.key === key`; any other key is `loading`, so a switch
 *   of campaign (or of anything else) shows the skeleton and never an old row (LIB-25).
 *   Requests are guarded by refs rather than effect cleanup, so StrictMode's second
 *   effect run neither doubles a request nor drops its answer, and an answer that
 *   arrives under an older key is dropped.
 * - **Search** is server-side (LIB-20): trimmed as the server trims, a single character
 *   is no search, and what is typed settles for 250 ms before it is sent. Clearing the
 *   field needs no wait. A search the contract refuses is `invalid` and sends nothing.
 *   It travels in the POST body and nowhere else (X-7): no URL, storage or log.
 * - **Paging** is 25 rows by the server's opaque cursor (LIB-23). Load more appends and
 *   de-duplicates; its failure keeps every row (STATE-1).
 * - The page echoes the campaign and category; one that does not match what was asked is
 *   an error (LIB-25).
 *
 * - **A second tab** (LIB-24): when this tab becomes visible again the first page is asked for
 *   once more, quietly. Nothing shows loading and no key changes, so no row is replaced by a
 *   skeleton and no focus is lost; the new first page replaces the old one, rows loaded past it
 *   stay, and a failed refresh leaves everything as it was.
 *
 * Nothing here logs or stores. Rows hold GM-private titles: they live in React memory.
 */

import * as React from 'react'
import {
  codePointLength, LibraryQuerySchema, SEARCH_MIN_CHARS, trimWire, type DocumentTypeId, type LibraryCategory, type LibraryItem,
  type LibraryQuery,
} from '../gm/contracts'
import { queryLibrary } from '../gm/documentApi'
import { useCampaign } from './campaignContext'
import { useCanvasState } from './canvasContext'

/** LIB-5: cues are not documents, so they are not a tab of the panel. */
export type LibraryCategoryId = Exclude<LibraryCategory, 'cues'>
export const LIBRARY_TABS: readonly LibraryCategoryId[] = ['npcs', 'bestiary', 'documents', 'session-log']

/** LIB-23. */
export const LIBRARY_PAGE_SIZE = 25
/** LIB-20: what is typed settles this long before it is sent. */
export const SEARCH_DEBOUNCE_MS = 250

export interface LibraryListQuery {
  readonly category: LibraryCategoryId
  /** The raw field text. */
  readonly search: string
  readonly sort: 'recent' | 'name'
  readonly archived: boolean
  /** Documents only: one of the types that live there, or null for all. */
  readonly type: DocumentTypeId | null
}

export type LibraryListState =
  | { readonly status: 'loading' }
  /** The contract refuses the search text: no request was made. */
  | { readonly status: 'invalid' }
  /** The first page failed: any non-ok answer, or a page that is not the one asked for. */
  | { readonly status: 'error' }
  | {
      readonly status: 'ready'
      readonly items: readonly LibraryItem[]
      readonly nextCursor: string | null
      readonly more: 'idle' | 'loading' | 'failed'
      /** The settled search this answer is for. */
      readonly search: string
      /** Counts first-page answers (not Load more, not a removal): a change is a new result to announce. */
      readonly generation: number
    }

export interface LibraryList {
  readonly state: LibraryListState
  /** From `error`: asks again with the same search, sort, filter and type. */
  retry(): void
  loadMore(): void
  /** Takes a row out in place (a restored document, LIB-24). */
  removeItem(documentId: string): void
}

interface Loaded {
  readonly key: string
  readonly status: 'ready' | 'error'
  readonly items: readonly LibraryItem[]
  readonly nextCursor: string | null
  readonly more: 'idle' | 'loading' | 'failed'
  readonly search: string
  readonly generation: number
}

/** The search as it would be sent: trimmed, and nothing at all below the two-character minimum. */
function effectiveSearch(raw: string): string {
  const trimmed = trimWire(raw)
  return codePointLength(trimmed) < SEARCH_MIN_CHARS ? '' : trimmed
}

/** The request body for a first page, or null when the contract refuses it. */
function firstPageBody(
  campaignId: string,
  category: LibraryCategoryId,
  archived: boolean,
  sort: 'recent' | 'name',
  type: DocumentTypeId | null,
  search: string,
): LibraryQuery | null {
  const parsed = LibraryQuerySchema.safeParse({
    schema_version: 1,
    campaign_id: campaignId,
    category,
    search,
    sort,
    archived,
    limit: LIBRARY_PAGE_SIZE,
    ...(category === 'documents' && type !== null ? { type } : {}),
  })
  return parsed.success ? parsed.data : null
}

function appendUnique(items: readonly LibraryItem[], more: readonly LibraryItem[]): readonly LibraryItem[] {
  const seen = new Set(items.map((item) => item.document_id))
  return [...items, ...more.filter((item) => !seen.has(item.document_id))]
}

export function useLibraryList(query: LibraryListQuery, fetchImpl?: typeof fetch): LibraryList {
  const { scope } = useCampaign()
  const { documentsVersion } = useCanvasState()
  const [retries, setRetries] = React.useState(0)
  const [loaded, setLoaded] = React.useState<Loaded | null>(null)

  const { category, archived, sort, type } = query
  const campaignId = scope?.campaignId ?? null
  const typed = effectiveSearch(query.search)

  // What has settled. Clearing needs no wait, so it is adjusted during render (never by a
  // setState inside an effect); a longer search waits out the debounce in the effect below.
  const [settled, setSettled] = React.useState(typed)
  if (typed === '' && settled !== '') setSettled('')
  const search = typed === '' ? '' : settled
  React.useEffect(() => {
    if (typed === settled) return
    const timer = setTimeout(() => setSettled(typed), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [typed, settled])

  const body = campaignId === null ? null : firstPageBody(campaignId, category, archived, sort, type, search)
  const requestKey =
    scope === null || body === null
      ? null
      : `${scope.key}|${category}|${archived}|${sort}|${type}|${search}|${documentsVersion}|${retries}`

  // The key the answers are for, as of the latest render: an answer that arrives under an older one
  // (a switch, a bump, a Retry, another search) is dropped.
  const latestKey = React.useRef<string | null>(requestKey)
  const requestedKey = React.useRef<string | null>(null)
  const bodyRef = React.useRef<LibraryQuery | null>(body)
  const generationRef = React.useRef(0)

  React.useEffect(() => {
    latestKey.current = requestKey
    bodyRef.current = body
    if (requestKey === null || body === null || requestedKey.current === requestKey) return
    requestedKey.current = requestKey
    const key = requestKey
    const { campaign_id: askedCampaign, category: askedCategory } = body
    void queryLibrary(body, fetchImpl).then((result) => {
      if (latestKey.current !== key) return
      if (
        result.kind !== 'ok' ||
        result.page.campaign_id !== askedCampaign ||
        result.page.category !== askedCategory
      ) {
        setLoaded({ key, status: 'error', items: [], nextCursor: null, more: 'idle', search: body.search, generation: 0 })
        return
      }
      generationRef.current += 1
      setLoaded({
        key, status: 'ready', items: result.page.items, nextCursor: result.page.next_cursor, more: 'idle',
        search: body.search, generation: generationRef.current,
      })
    })
  }, [requestKey, body, fetchImpl])

  const read = loaded !== null && loaded.key === requestKey ? loaded : null
  const readRef = React.useRef<Loaded | null>(read)
  React.useLayoutEffect(() => {
    readRef.current = read
  })

  const loadMore = React.useCallback((): void => {
    const current = readRef.current
    const first = bodyRef.current
    const key = latestKey.current
    if (current === null || first === null || key === null || current.status !== 'ready') return
    if (current.nextCursor === null || current.more === 'loading') return
    const cursor = current.nextCursor
    setLoaded({ ...current, more: 'loading' })
    readRef.current = { ...current, more: 'loading' }
    void queryLibrary({ ...first, cursor }, fetchImpl).then((result) => {
      if (latestKey.current !== key) return
      setLoaded((now) => {
        if (now === null || now.key !== key) return now
        if (
          result.kind !== 'ok' ||
          result.page.campaign_id !== first.campaign_id ||
          result.page.category !== first.category
        ) {
          return { ...now, more: 'failed' }
        }
        return {
          ...now, items: appendUnique(now.items, result.page.items), nextCursor: result.page.next_cursor, more: 'idle',
        }
      })
    })
  }, [fetchImpl])

  React.useEffect(() => {
    function onVisible(): void {
      if (document.visibilityState !== 'visible') return
      const current = readRef.current
      const first = bodyRef.current
      const key = latestKey.current
      if (current === null || first === null || key === null || current.status !== 'ready' || current.more === 'loading') return
      void queryLibrary(first, fetchImpl).then((result) => {
        if (latestKey.current !== key) return
        if (result.kind !== 'ok' || result.page.campaign_id !== first.campaign_id || result.page.category !== first.category) return
        const page = result.page
        setLoaded((now) => {
          if (now === null || now.key !== key || now.status !== 'ready' || now.more === 'loading') return now
          const beyond = now.items.length > LIBRARY_PAGE_SIZE
          return {
            ...now,
            items: beyond ? appendUnique(page.items, now.items.slice(LIBRARY_PAGE_SIZE)) : page.items,
            nextCursor: beyond ? now.nextCursor : page.next_cursor,
          }
        })
      })
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [fetchImpl])

  const removeItem = React.useCallback((documentId: string): void => {
    setLoaded((now) => {
      if (now === null || now.key !== latestKey.current) return now
      return { ...now, items: now.items.filter((item) => item.document_id !== documentId) }
    })
  }, [])

  const retry = React.useCallback((): void => setRetries((count) => count + 1), [])

  let state: LibraryListState
  if (body === null && campaignId !== null) state = { status: 'invalid' }
  else if (read === null) state = { status: 'loading' }
  else if (read.status === 'error') state = { status: 'error' }
  else {
    state = {
      status: 'ready', items: read.items, nextCursor: read.nextCursor, more: read.more, search: read.search,
      generation: read.generation,
    }
  }

  return { state, retry, loadMore, removeItem }
}
