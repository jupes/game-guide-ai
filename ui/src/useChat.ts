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

export type PostFn = (
  prompt: string, mode: ChatMode, conversationId: string | null, modelPreference: string,
) => Promise<ChatResult>
export type LoadHistoryFn = (conversationId: string) => Promise<MessagesResult>
const monotonicNow = () => performance.now()

export interface UseChatOptions {
  post?: PostFn
  loadHistory?: LoadHistoryFn
  mode: ChatMode
  conversationId: string | null
  /** "auto" or a specific enabled catalog alias (b8o.2) — the conversation's
   * bound preference, threaded through to `post` unchanged. Defaults to
   * "auto", matching the backend's own default for an omitted preference. */
  modelPreference?: string
  now?: () => number
  recordMetric?: (point: MetricPoint) => void
  /** Called when the SERVER supplied the conversation id (x5bz.3.2).
   *
   * Sending with a null id no longer means "this turn is unrecorded" — the
   * server mints one and persists under it. If the client kept discarding that
   * id, every message from the landing screen would open a fresh conversation
   * the user could never return to. Adopting it is what makes the id the server
   * chose the one the next turn continues. */
  onConversationAdopted?: (conversationId: string) => void
  /** Called when a turn's response says the SERVER rebound this conversation
   * off a manual pick it has since retired (agent-forge-harness-j9w) —
   * `routing.fallback_from` set, `preference` is `routing.effective`. The
   * one legitimate way `boundPreference` changes after the first prompt;
   * wire it to `ConversationStore.rebindPreference` so the next turn stops
   * sending the retired id and ModelPicker shows what actually answered. */
  onPreferenceRebound?: (conversationId: string, preference: string) => void
  /** Called exactly once per turn THIS hook sent, right after the commit in
   * which it settles (agent-forge-harness-ekf) — 'done' for an answer, 'error'
   * for a failed result or a rejection. Additive and optional: every existing
   * caller is unaffected. This is the seam a consumer uses to announce arrival
   * without re-deriving it from `exchanges` (a recalled turn and a settled
   * turn both end up `status: 'done'` with ids the consumer cannot tell apart).
   *
   * The second argument is the conversation id the turn was SENT for — not
   * necessarily the one this hook is scoped to right now
   * (agent-forge-harness-swg / pr114 M-1).
   *
   * The third, `shown`, is whether this settle is actually on screen: true only
   * when the settle's own state update found its exchange and wrote the
   * outcome into it (so it was APPLIED), and that exchange is among the
   * exchanges this hook is returning at the commit that follows (so it is
   * DRAWN). Comparing conversation ids alone cannot tell — after A -> B -> A
   * the ids match again, but A's recall has replaced the exchange list and
   * the settle writes nothing (agent-forge-harness-swg / pr129 M-1). The one
   * exception is a turn sent with no id whose server-minted id the consumer
   * has adopted (x5bz.3.2): that is the same conversation, not a switch away,
   * so it counts as shown even while the adopted id's recall re-reads it. A
   * consumer that announces arrival must announce only when `shown` is true. */
  onTurnSettled?: (outcome: 'done' | 'error', conversationId: string | null, shown: boolean) => void
}

/** The latest settle of a turn this hook sent (agent-forge-harness-swg /
 * pr129 M-1). Recorded by `settle`'s own state update, so whether it was
 * applied is decided against the exact state it landed on rather than against
 * a snapshot taken before other queued updates (a recall, say) are processed. */
interface SettleRecord {
  exchangeId: number
  outcome: 'done' | 'error'
  /** The conversation the turn was sent for. */
  sentFor: string | null
  /** The id the server minted for a turn sent with none (x5bz.3.2), else null. */
  adoptedId: string | null
  /** The update found the exchange in the scope it was sent for and wrote it. */
  applied: boolean
}

interface ChatState {
  /** The conversationId these exchanges belong to. */
  scopeId: string | null
  exchanges: Exchange[]
  /** Non-null when the history recall for this scope failed. */
  historyError: string | null
  loadingHistory: boolean
  /** Carried through every update so a settle queued alongside a recall is
   * never lost; reported once, by identity (see the effect in `useChat`). */
  lastSettle: SettleRecord | null
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

// postChat's own params keep fetchImpl in test call sites' existing 4th
// position (real signature: prompt, mode, conversationId, fetchImpl,
// modelPreference) — this adapter is what lets the default `post` satisfy
// PostFn's (prompt, mode, conversationId, modelPreference) shape without
// reordering postChat's params and breaking every existing direct caller.
const defaultPost: PostFn = (prompt, mode, conversationId, modelPreference) =>
  postChat(prompt, mode, conversationId, undefined, modelPreference)

export function useChat({
  post = defaultPost,
  loadHistory = getMessages,
  mode,
  conversationId,
  modelPreference = 'auto',
  onConversationAdopted,
  onPreferenceRebound,
  onTurnSettled,
  now = monotonicNow,
  recordMetric = recordBrowserMetric,
}: UseChatOptions) {
  const [state, setState] = useState<ChatState>({
    scopeId: conversationId,
    exchanges: [],
    historyError: null,
    // A conversation opened at mount is loading until the recall effect settles.
    loadingHistory: conversationId !== null,
    lastSettle: null,
  })
  const pendingRef = useRef(false)
  const nextId = useRef(1)
  /** The settle `onTurnSettled` last reported — each is reported exactly once. */
  const reportedSettle = useRef<SettleRecord | null>(null)

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
              lastSettle: prev.lastSettle,
            }
          }
          return {
            scopeId: conversationId,
            exchanges: live,
            historyError: result.message,
            loadingHistory: false,
            lastSettle: prev.lastSettle,
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
          lastSettle: prev.lastSettle,
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
        lastSettle: prev.lastSettle,
      }))

      const settle = (
        update: Partial<Exchange> & { status: 'done' | 'error' },
        // 'throttled' is deliberately its own outcome rather than folding into
        // http_error: it is the cost guard doing its job, not the service
        // failing, and the metric is the only place an operator would see the
        // limit actually biting in production.
        outcome:
          | 'success' | 'http_error' | 'network_error' | 'aborted' | 'throttled'
          | 'conversation_mismatch',
        adoptedId: string | null = null,
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
        setState((prev) => {
          // The user may have switched to (and recalled) a different
          // conversation while this turn was in flight. Never stamp scopeId
          // back to the conversation this turn was SENT from — that would
          // clobber the scope the user has since moved to and strand it on
          // "Recalling the conversation…" forever, since nothing would ever
          // change the recall effect's deps again (agent-forge-harness-4pg).
          // A turn that settles after its conversation was left is dropped
          // here, same as the recall effect drops a stale response — and so is
          // one whose exchange a recall has since replaced (A -> B -> A: the
          // scope matches again, but there is nothing left to write into).
          // `conversationId` here is THIS send's own closure — the
          // conversation the turn was sent for (agent-forge-harness-swg).
          const applied =
            prev.scopeId === conversationId && prev.exchanges.some((e) => e.id === id)
          const lastSettle: SettleRecord = {
            exchangeId: id,
            outcome: update.status,
            sentFor: conversationId,
            adoptedId,
            applied,
          }
          if (!applied) return { ...prev, lastSettle }
          return {
            ...prev,
            exchanges: prev.exchanges.map((e) => (e.id === id ? { ...e, ...update } : e)),
            lastSettle,
          }
        })
      }

      void post(trimmed, mode, conversationId, modelPreference).then(
        (result) => {
          if (result.kind === 'ok') {
            // Only when we had none: a server echo of the id we sent is not an
            // adoption, and re-announcing it would churn navigation state on
            // every single turn.
            const supplied = result.response.conversation_id
            const adopted =
              conversationId === null && typeof supplied === 'string' && supplied ? supplied : null
            if (adopted !== null) {
              onConversationAdopted?.(adopted)
            }
            const routing = result.response.routing
            const scopeId = conversationId ?? adopted
            if (scopeId !== null && typeof routing?.fallback_from === 'string' && routing.fallback_from) {
              onPreferenceRebound?.(scopeId, routing.effective)
            }
            settle({ status: 'done', response: result.response }, 'success', adopted)
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
    [
      post, mode, conversationId, modelPreference, now, recordMetric,
      onConversationAdopted, onPreferenceRebound,
    ],
  )

  // agent-forge-harness-ekf / agent-forge-harness-swg (pr129 M-1): report each
  // settle exactly once, after the commit that carries it — never from a
  // recall or a re-render (a record is reported by identity, and recalls only
  // carry the existing one through). Whether it is SHOWN is decided here, at
  // that commit, against the exchanges this render actually returns.
  const { lastSettle, exchanges: scopeExchanges } = state
  useEffect(() => {
    const s = lastSettle
    if (s === null || reportedSettle.current === s) return
    reportedSettle.current = s
    // Drawn: the exchange is among those this render returns (`exchanges`
    // above is `scopeExchanges` exactly when `scoped`).
    const drawn = scoped && scopeExchanges.some((e) => e.id === s.exchangeId)
    const shown =
      s.applied && (drawn || (s.adoptedId !== null && conversationId === s.adoptedId))
    onTurnSettled?.(s.outcome, s.sentFor, shown)
  }, [lastSettle, scoped, scopeExchanges, conversationId, onTurnSettled])

  return { exchanges, send, pending, historyError, loadingHistory }
}
