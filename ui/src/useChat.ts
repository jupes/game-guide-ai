/**
 * Chat state hook — owns the client-side exchange list. Live sends go through
 * `post`; opening a conversation recalls its stored history through
 * `loadHistory` (channel-chats CP-B) and seeds it ahead of anything sent while
 * the recall was in flight. One in-flight request at a time; empty prompts are
 * ignored.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { getMessages, postChat } from './api'
import type { ChatResponse, ChatResult, ChatMode, MessagesResult, StoredMessage } from './api'
import {
  recordMetric as recordBrowserMetric,
  runtimeMetricLabels,
  type MetricPoint,
} from './metrics/metrics'

export type ExchangeStatus = 'pending' | 'done' | 'error'

export interface Exchange {
  id: number
  prompt: string
  status: ExchangeStatus
  response?: ChatResponse
  error?: string
}

export type PostFn = (prompt: string, mode: ChatMode, conversationId: string | null) => Promise<ChatResult>
export type LoadHistoryFn = (conversationId: string) => Promise<MessagesResult>
const monotonicNow = () => performance.now()

export interface UseChatOptions {
  post?: PostFn
  loadHistory?: LoadHistoryFn
  mode: ChatMode
  conversationId: string | null
  now?: () => number
  recordMetric?: (point: MetricPoint) => void
}

interface ChatState {
  /** The conversationId these exchanges belong to. */
  scopeId: string | null
  exchanges: Exchange[]
  /** Non-null when the history recall for this scope failed. */
  historyError: string | null
  loadingHistory: boolean
}

/** Pair stored rows into display exchanges. A `user` row opens an exchange;
 * the following `assistant` row completes it. Orphan assistant rows (their
 * user turn fell off the load-limit window, or the answer errored and was
 * never stored) are skipped rather than rendered as empty player bubbles. */
function toExchanges(messages: StoredMessage[], nextId: { current: number }): Exchange[] {
  const out: Exchange[] = []
  for (const m of messages) {
    if (m.role === 'user') {
      out.push({ id: nextId.current++, prompt: m.content, status: 'done' })
    } else {
      const last = out[out.length - 1]
      if (last && last.response === undefined && last.status === 'done') {
        last.response = {
          answer: m.content,
          sources: [],
          answerable: true,
          suggestions: m.suggestions ?? null,
        }
      }
    }
  }
  // A trailing user row with no stored answer still renders its prompt.
  return out
}

export function useChat({
  post = postChat,
  loadHistory = getMessages,
  mode,
  conversationId,
  now = monotonicNow,
  recordMetric = recordBrowserMetric,
}: UseChatOptions) {
  const [state, setState] = useState<ChatState>({
    scopeId: conversationId,
    exchanges: [],
    historyError: null,
    // A conversation opened at mount is loading until the recall effect settles.
    loadingHistory: conversationId !== null,
  })
  const pendingRef = useRef(false)
  const nextId = useRef(1)

  // Derive the visible exchanges: if the scope has changed, treat as empty
  // (and loading) until the recall effect below re-seeds. Pure derivation —
  // the effect only does async work; it never sets state synchronously.
  const scoped = state.scopeId === conversationId
  const exchanges = scoped ? state.exchanges : []
  const historyError = scoped ? state.historyError : null
  const loadingHistory = scoped ? state.loadingHistory : conversationId !== null

  const pending = exchanges.some((e) => e.status === 'pending')

  // Recall stored history when a conversation opens. Seeded rows land BEFORE
  // any exchange sent while the recall was in flight; a stale response for a
  // conversation we've already left is dropped.
  useEffect(() => {
    if (conversationId === null) return
    let cancelled = false
    void loadHistory(conversationId).then(
      (result) => {
        if (cancelled) return
        setState((prev) => {
          const live = prev.scopeId === conversationId ? prev.exchanges : []
          if (result.kind === 'ok') {
            return {
              scopeId: conversationId,
              exchanges: [...toExchanges(result.messages, nextId), ...live],
              historyError: null,
              loadingHistory: false,
            }
          }
          return {
            scopeId: conversationId,
            exchanges: live,
            historyError: result.message,
            loadingHistory: false,
          }
        })
      },
      // A rejecting LoadHistoryFn degrades the same as an error result.
      (err: unknown) => {
        if (cancelled) return
        setState((prev) => ({
          scopeId: conversationId,
          exchanges: prev.scopeId === conversationId ? prev.exchanges : [],
          historyError: err instanceof Error ? err.message : 'Message history unavailable.',
          loadingHistory: false,
        }))
      },
    )
    return () => {
      cancelled = true
    }
  }, [conversationId, loadHistory])

  const send = useCallback(
    (prompt: string) => {
      const trimmed = prompt.trim()
      if (!trimmed || pendingRef.current) return
      pendingRef.current = true
      const startedAt = now()

      const id = nextId.current++
      setState((prev) => ({
        scopeId: conversationId,
        exchanges: [
          ...(prev.scopeId === conversationId ? prev.exchanges : []),
          { id, prompt: trimmed, status: 'pending' },
        ],
        historyError: prev.scopeId === conversationId ? prev.historyError : null,
        // Entering a new scope via send(): its recall may still be in flight.
        loadingHistory:
          prev.scopeId === conversationId ? prev.loadingHistory : conversationId !== null,
      }))

      const settle = (
        update: Partial<Exchange>,
        outcome: 'success' | 'http_error' | 'network_error' | 'aborted',
      ) => {
        pendingRef.current = false
        const labels = runtimeMetricLabels(mode)
        recordMetric({
          name: 'ui.interaction.chat_round_trip_ms',
          kind: 'numeric',
          unit: 'ms',
          value: Math.max(0, now() - startedAt),
          labels,
        })
        recordMetric({
          name: 'ui.interaction.chat_outcome',
          kind: 'categorical',
          unit: 'category',
          value: outcome,
          labels,
        })
        setState((prev) => ({
          ...prev,
          scopeId: conversationId,
          exchanges: prev.exchanges.map((e) => (e.id === id ? { ...e, ...update } : e)),
        }))
      }

      void post(trimmed, mode, conversationId).then(
        (result) => {
          if (result.kind === 'ok') {
            settle({ status: 'done', response: result.response }, 'success')
          } else {
            settle(
              { status: 'error', error: result.message },
              result.outcome ?? 'http_error',
            )
          }
        },
        // A custom PostFn may reject; don't strand pendingRef (locks the composer).
        (err: unknown) => {
          settle(
            {
              status: 'error',
              error:
                err instanceof Error
                  ? err.message
                  : 'Unexpected error — please try again.',
            },
            err instanceof Error && err.name === 'AbortError'
              ? 'aborted'
              : 'network_error',
          )
        },
      )
    },
    [post, mode, conversationId, now, recordMetric],
  )

  return { exchanges, send, pending, historyError, loadingHistory }
}
