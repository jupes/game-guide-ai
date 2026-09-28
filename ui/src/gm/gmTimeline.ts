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
 * **Load earlier**, a follow-up.
 *
 * Privacy (X-7): prompts, briefs and answers are rendered and nothing else. A
 * React key is an entry id or a local counter, never text.
 */

import { useEffect, useState } from 'react'
import { getTimelinePage } from '../api'
import type { ChatResponse, TimelinePageResult } from '../api'
import type { Exchange } from '../useChat'
import { TIMELINE_PAGE_MAX_ITEMS } from './contracts'
import type { ChatAnswer, TimelineItem, ToolInvocation } from './contracts'

export type LoadTimelinePageFn = (conversationId: string, cursor: string | null) => Promise<TimelinePageResult>

// ── The lanes ────────────────────────────────────────────────────────────────

/** A plain turn's outcome, as the lane shows it (RAIL-14). `answerable` and
 * `sources` are `null` when a turn predates the durable timeline and they were
 * never recorded — shown as unknown, never as grounded. */
export type LaneAnswer = Pick<ChatAnswer, 'text' | 'answerable' | 'sources' | 'spell_content' | 'stat_block'>

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
  | { kind: 'ok'; items: TimelineItem[] }
  | { kind: 'error'; message: string }

/** The newest entries of a conversation, oldest first for display. */
export async function readTimeline(conversationId: string, loadPage: LoadTimelinePageFn): Promise<TimelineRead> {
  const newestFirst: TimelineItem[] = []
  let cursor: string | null = null
  for (let page = 0; page < MAX_PAGES_PER_READ; page += 1) {
    const result = await loadPage(conversationId, cursor)
    // Nothing this user can read yet — a conversation made in the sidebar is
    // not on the server until its first turn.
    if (result.kind === 'missing') break
    if (result.kind === 'error') return result
    newestFirst.push(...result.page.items)
    const next = result.page.next_cursor
    // Only a null cursor ends the list; a short or empty page does not.
    if (next === null || next === cursor || newestFirst.length >= HYDRATE_TARGET) break
    cursor = next
  }
  return { kind: 'ok', items: newestFirst.reverse() }
}

export interface GmTimeline {
  /** Oldest first. */
  items: readonly TimelineItem[]
  loading: boolean
  /** A failed read: a system message above a thread that still works (§12.2). */
  error: string | null
}

interface TimelineState extends GmTimeline {
  scopeId: string | null
}

const NO_TIMELINE: GmTimeline = { items: [], loading: false, error: null }
const LOADING: GmTimeline = { items: [], loading: true, error: null }

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

  useEffect(() => {
    if (scope === null) return
    let cancelled = false
    void readTimeline(scope, loadPage).then(
      (read) => {
        if (cancelled) return
        setState(
          read.kind === 'ok'
            ? { scopeId: scope, items: read.items, loading: false, error: null }
            : { scopeId: scope, items: [], loading: false, error: read.message },
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
        })
      },
    )
    return () => {
      cancelled = true
    }
  }, [scope, loadPage])

  if (scope === null) return NO_TIMELINE
  return state.scopeId === scope ? state : LOADING
}
