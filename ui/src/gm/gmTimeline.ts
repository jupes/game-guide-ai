/**
 * The GM thread's model (1kg.3.4): what the transcript reads, and how a stored
 * entry and a live turn become the same three lanes.
 *
 * **One model for both sources.** A turn hydrated from
 * `GET /conversations/{id}/timeline` and a turn this pane just sent through
 * `/chat` are mapped into the same `GmTurn`, and `GmThread` renders only that —
 * so a reload cannot show an answer differently from how it arrived.
 *
 * **Paging.** A timeline page may be short, or empty, while `next_cursor` is
 * still non-null (the contract's *Pagination*, the bead's 2026-09-25 note): the
 * walk follows the cursor through such pages and only a `null` cursor ends the
 * list. It stops early once it holds a full page's worth of entries — already
 * twice the exchanges `/messages` recalls — and reading further back is
 * **Load earlier** (1kg.3.6): `useGmTimeline` keeps the cursor the initial
 * read stopped on and continues the same walk from it, prepending what it
 * finds above what is already drawn. The kept cursor lives only in memory
 * (X-7) — never in state, so it cannot be inspected between renders, and
 * never persisted.
 *
 * Privacy (X-7): prompts, briefs and answers are rendered and nothing else. A
 * React key is an entry id or a local counter, never text.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { getTimelinePage } from '../api'
import type { ChatResponse, TimelinePageResult } from '../api'
import type { Exchange } from '../useChat'
import { TIMELINE_PAGE_MAX_ITEMS } from './contracts'
import type { ChatAnswer, TimelineItem, ToolInvocation } from './contracts'

export type LoadTimelinePageFn = (conversationId: string, cursor: string | null) => Promise<TimelinePageResult>

// ── The lanes ────────────────────────────────────────────────────────────────

/** A plain turn's outcome, as the lane shows it (RAIL-14). `answerable` and
 * `sources` are `null` when a turn predates the durable timeline and they were
 * never recorded — shown as unknown, never as grounded. `suggestions` are a
 * spell answer's usage ideas; a mode chip keeps the same conversation, so a
 * spell entry can be hydrated into this thread (agent-forge-harness-0ru). */
export type LaneAnswer = Pick<
  ChatAnswer,
  'text' | 'answerable' | 'sources' | 'spell_content' | 'stat_block' | 'suggestions'
>

export type AnswerState =
  /** Hydrated with no stored answer: the turn failed, or is running elsewhere. */
  | { state: 'none' }
  | { state: 'pending' }
  | { state: 'answered'; answer: LaneAnswer }
  | { state: 'failed'; message: string }

/**
 * One exchange of the GM thread: the GM's turn, then its outcome beneath it.
 * v1 has no player or table turns (E-5, NG-12), so there is no player-lane
 * member to render.
 */
export type GmTurn =
  /** RAIL-14. `prompt` is `null` only for an old answer whose prompt was never recorded. */
  | { kind: 'chat'; key: string; prompt: string | null; answer: AnswerState }
  /** RAIL-9: a tool and a brief, never the slash string. */
  | { kind: 'tool'; key: string; entryId: string; brief: string; invocation: ToolInvocation }
  /** An `opaque` entry or one this client cannot read: RAIL-24's placeholder (X-8). */
  | { kind: 'unreadable'; key: string }
  /** An AI edit. Known, but its lane is 1kg.6.5's, so it says so rather than guessing. */
  | { kind: 'unsupported'; key: string }

/** The stored entries, oldest first, as turns. Session dividers are 1kg.3.5's to label. */
export function turnsFromTimeline(items: readonly TimelineItem[]): GmTurn[] {
  const turns: GmTurn[] = []
  items.forEach((item, index) => {
    if (item.kind === 'unknown') {
      turns.push({ kind: 'unreadable', key: item.entry_id === null ? `unknown:${index}` : `entry:${item.entry_id}` })
      return
    }
    const entry = item.value
    const key = `entry:${entry.entry_id}`
    switch (entry.entry_kind) {
      case 'chat':
        turns.push({
          kind: 'chat',
          key,
          prompt: entry.prompt,
          answer: entry.answer === null ? { state: 'none' } : { state: 'answered', answer: entry.answer },
        })
        return
      case 'tool':
        turns.push({ kind: 'tool', key, entryId: entry.entry_id, brief: entry.brief, invocation: entry.invocation })
        return
      case 'edit':
        turns.push({ kind: 'unsupported', key })
        return
      case 'opaque':
        turns.push({ kind: 'unreadable', key })
        return
      case 'session_divider':
        return
    }
  })
  return turns
}

/** `/chat`'s answer in the timeline's words, so a live turn renders as its reload will. */
export function answerFromResponse(response: ChatResponse): LaneAnswer {
  return {
    text: response.answer,
    answerable: response.answerable,
    sources: response.sources,
    spell_content: response.spell_content,
    stat_block: response.stat_block,
    suggestions: response.suggestions,
  }
}

/** A turn this pane sent, at whatever stage it has reached. */
export function turnFromExchange(exchange: Exchange): GmTurn {
  const key = `live:${exchange.id}`
  if (exchange.status === 'pending') return { kind: 'chat', key, prompt: exchange.prompt, answer: { state: 'pending' } }
  if (exchange.status === 'error') {
    return { kind: 'chat', key, prompt: exchange.prompt, answer: { state: 'failed', message: exchange.error ?? '' } }
  }
  return {
    kind: 'chat',
    key,
    prompt: exchange.prompt,
    answer: exchange.response ? { state: 'answered', answer: answerFromResponse(exchange.response) } : { state: 'none' },
  }
}

/**
 * The stored plain turns in the shape chat export writes today, so exporting a
 * GM thread still carries its history. What a turn never recorded is written as
 * today's recall writes it (`useChat`'s `toExchanges`); a lossless GM-thread
 * export, with documents as links only, is EXPORT-12's.
 */
export function exchangesForExport(items: readonly TimelineItem[]): Exchange[] {
  const out: Exchange[] = []
  for (const item of items) {
    if (item.kind !== 'ok' || item.value.entry_kind !== 'chat') continue
    const { prompt, answer } = item.value
    out.push({
      id: out.length,
      prompt: prompt ?? '',
      status: 'done',
      ...(answer === null
        ? {}
        : { response: { answer: answer.text, sources: answer.sources ?? [], answerable: answer.answerable ?? true } }),
    })
  }
  return out
}

// ── Reading the timeline ─────────────────────────────────────────────────────

/** Where the first read stops: one full page of entries. */
export const HYDRATE_TARGET = TIMELINE_PAGE_MAX_ITEMS
/** A bound on one walk. The server bounds a run of empty pages; this only
 * stops a client spinning against one that does not. */
export const MAX_PAGES_PER_READ = 50

export type TimelineRead =
  | { kind: 'ok'; items: TimelineItem[]; cursor: string | null }
  | { kind: 'error'; message: string }

/**
 * The shared walk (1kg.3.4's initial hydrate and 1kg.3.6's Load earlier are
 * the same walk from a different starting cursor). Stops at `target` items,
 * at a `missing` conversation, or once only a `null` cursor is left to try —
 * never on a short or empty page, which is not the end of the list (the
 * contract's *Pagination*, the bead's 2026-09-25 note). The returned cursor
 * is where a later walk should resume: `null` only when the list truly ended
 * or a server that hands back the same cursor forced the spin guard below.
 */
async function walkTimeline(
  conversationId: string,
  loadPage: LoadTimelinePageFn,
  startCursor: string | null,
  target: number,
): Promise<TimelineRead> {
  const newestFirst: TimelineItem[] = []
  let cursor: string | null = startCursor
  for (let page = 0; page < MAX_PAGES_PER_READ; page += 1) {
    const result = await loadPage(conversationId, cursor)
    // Nothing this user can read yet — a conversation made in the sidebar is
    // not on the server until its first turn.
    if (result.kind === 'missing') {
      cursor = null
      break
    }
    if (result.kind === 'error') return result
    newestFirst.push(...result.page.items)
    const next = result.page.next_cursor
    // A server that hands back the cursor it was just given cannot be walked
    // further; treat it as ended rather than spin against it forever.
    if (next === cursor) {
      cursor = null
      break
    }
    cursor = next
    if (cursor === null || newestFirst.length >= target) break
  }
  return { kind: 'ok', items: newestFirst.reverse(), cursor }
}

/** The newest entries of a conversation, oldest first for display. */
export async function readTimeline(conversationId: string, loadPage: LoadTimelinePageFn): Promise<TimelineRead> {
  return walkTimeline(conversationId, loadPage, null, HYDRATE_TARGET)
}

/** One hop of Load earlier (1kg.3.6): continues the walk from the cursor the
 * last read (or the last Load earlier) stopped on. */
export const LOAD_EARLIER_TARGET = HYDRATE_TARGET

export async function readEarlierTimeline(
  conversationId: string,
  cursor: string,
  loadPage: LoadTimelinePageFn,
): Promise<TimelineRead> {
  return walkTimeline(conversationId, loadPage, cursor, LOAD_EARLIER_TARGET)
}

/** An item's stable identity for de-duplication, or `null` when it has none
 * (an unreadable entry the server never gave an id). */
function entryKeyOf(item: TimelineItem): string | null {
  return item.kind === 'ok' ? item.value.entry_id : item.entry_id
}

/**
 * Prepends `older` above `existing`, oldest first — and never draws a turn
 * already on screen (1kg.3.6): correct cursor bookkeeping should already
 * make the two runs disjoint, but this is the one place that would notice if
 * it didn't. An item with no id (an unreadable entry) is never treated as a
 * duplicate of another — there is nothing to compare it by.
 */
export function mergeEarlierItems(
  existing: readonly TimelineItem[],
  older: readonly TimelineItem[],
): TimelineItem[] {
  const seen = new Set<string>()
  for (const item of existing) {
    const key = entryKeyOf(item)
    if (key !== null) seen.add(key)
  }
  const deduped: TimelineItem[] = []
  for (const item of older) {
    const key = entryKeyOf(item)
    if (key !== null) {
      if (seen.has(key)) continue
      seen.add(key)
    }
    deduped.push(item)
  }
  return [...deduped, ...existing]
}

export interface GmTimeline {
  /** Oldest first. */
  items: readonly TimelineItem[]
  loading: boolean
  /** A failed read: a system message above a thread that still works (§12.2). */
  error: string | null
  /** An older page exists to walk to (1kg.3.6): the kept cursor is non-null. */
  hasEarlier: boolean
  /** A Load earlier walk is in flight. */
  loadingEarlier: boolean
  /** A failed Load earlier walk (§12.2). STATE-1: `items` is untouched. */
  earlierError: string | null
  /** Continues the walk from the kept cursor, prepending older entries above
   * what is already drawn. Retries in place on a previous failure (§12.2's
   * GM-thread row) — a no-op while one is already in flight or none remain. */
  loadEarlier: () => void
}

/** The part of `GmTimeline` that is plain state; `loadEarlier` is bound fresh
 * on every call so it always closes over the current scope. */
type TimelineSnapshot = Omit<GmTimeline, 'loadEarlier'>

interface TimelineState extends TimelineSnapshot {
  scopeId: string | null
}

const NO_TIMELINE: TimelineSnapshot = {
  items: [],
  loading: false,
  error: null,
  hasEarlier: false,
  loadingEarlier: false,
  earlierError: null,
}
const LOADING: TimelineSnapshot = {
  items: [],
  loading: true,
  error: null,
  hasEarlier: false,
  loadingEarlier: false,
  earlierError: null,
}
const NOOP = (): void => {}

/**
 * The stored thread of the open conversation, read when it opens. `enabled` is
 * false outside the GM channel, where nothing is fetched. State is scoped the
 * way `useChat` scopes it: derived, never reset by a synchronous setState.
 */
export function useGmTimeline(
  conversationId: string | null,
  enabled: boolean,
  loadPage: LoadTimelinePageFn = getTimelinePage,
): GmTimeline {
  const scope = enabled ? conversationId : null
  const [state, setState] = useState<TimelineState>({ scopeId: null, ...NO_TIMELINE })
  // Load earlier's cursor and in-flight guard live only in memory (X-7),
  // never in state or persisted — so neither a rerender nor a stale promise
  // can smear them across a conversation switch. `generationRef` counts the
  // hydrate effect's reads: each one resets the cursor and the guard, and a
  // Load earlier walk only lands in the generation it started in. A scope
  // string is not enough — A → B → A returns to the same string while the
  // first visit's walk is still out (review M1).
  const generationRef = useRef(0)
  const cursorRef = useRef<string | null>(null)
  const loadingEarlierRef = useRef(false)

  useEffect(() => {
    generationRef.current += 1
    cursorRef.current = null
    loadingEarlierRef.current = false
    if (scope === null) return
    let cancelled = false
    void readTimeline(scope, loadPage).then(
      (read) => {
        if (cancelled) return
        if (read.kind === 'ok') cursorRef.current = read.cursor
        setState(
          read.kind === 'ok'
            ? {
                scopeId: scope,
                items: read.items,
                loading: false,
                error: null,
                hasEarlier: read.cursor !== null,
                loadingEarlier: false,
                earlierError: null,
              }
            : {
                scopeId: scope,
                items: [],
                loading: false,
                error: read.message,
                hasEarlier: false,
                loadingEarlier: false,
                earlierError: null,
              },
        )
      },
      // A rejecting loader degrades like an error result.
      (err: unknown) => {
        if (cancelled) return
        setState({
          scopeId: scope,
          items: [],
          loading: false,
          error: err instanceof Error ? err.message : 'Message history unavailable.',
          hasEarlier: false,
          loadingEarlier: false,
          earlierError: null,
        })
      },
    )
    return () => {
      cancelled = true
    }
  }, [scope, loadPage])

  const loadEarlier = useCallback(() => {
    if (scope === null || loadingEarlierRef.current) return
    const cursor = cursorRef.current
    if (cursor === null) return
    const generation = generationRef.current
    loadingEarlierRef.current = true
    setState((prev) => (prev.scopeId === scope ? { ...prev, loadingEarlier: true, earlierError: null } : prev))
    void readEarlierTimeline(scope, cursor, loadPage).then(
      (read) => {
        // Superseded by a newer read (a switch, or a switch and back): the
        // hydrate effect has already reset the cursor and the guard for it,
        // and this answer belongs to nothing still on screen. The guard is
        // left alone — it is the newer read's now, and may be holding a walk
        // of its own.
        if (generationRef.current !== generation) return
        loadingEarlierRef.current = false
        if (read.kind === 'error') {
          setState((prev) => (prev.scopeId === scope ? { ...prev, loadingEarlier: false, earlierError: read.message } : prev))
          return
        }
        cursorRef.current = read.cursor
        setState((prev) =>
          prev.scopeId === scope
            ? {
                ...prev,
                items: mergeEarlierItems(prev.items, read.items),
                hasEarlier: read.cursor !== null,
                loadingEarlier: false,
                earlierError: null,
              }
            : prev,
        )
      },
      (err: unknown) => {
        if (generationRef.current !== generation) return
        loadingEarlierRef.current = false
        setState((prev) =>
          prev.scopeId === scope
            ? { ...prev, loadingEarlier: false, earlierError: err instanceof Error ? err.message : 'Message history unavailable.' }
            : prev,
        )
      },
    )
  }, [scope, loadPage])

  if (scope === null) return { ...NO_TIMELINE, loadEarlier: NOOP }
  return state.scopeId === scope ? { ...state, loadEarlier } : { ...LOADING, loadEarlier }
}
