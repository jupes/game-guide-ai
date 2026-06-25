/**
 * Chat state hook — owns the client-side exchange list (the service is
 * stateless). One in-flight request at a time; empty prompts are ignored.
 */

import { useCallback, useRef, useState } from 'react'
import { postChat } from './api'
import type { ChatResponse, ChatResult, ChatMode } from './api'

export type ExchangeStatus = 'pending' | 'done' | 'error'

export interface Exchange {
  id: number
  prompt: string
  status: ExchangeStatus
  response?: ChatResponse
  error?: string
}

export type PostFn = (prompt: string, mode: ChatMode, conversationId: string | null) => Promise<ChatResult>

export interface UseChatOptions {
  post?: PostFn
  mode: ChatMode
  conversationId: string | null
}

interface ChatState {
  /** The conversationId these exchanges belong to. */
  scopeId: string | null
  exchanges: Exchange[]
}

export function useChat({ post = postChat, mode, conversationId }: UseChatOptions) {
  const [state, setState] = useState<ChatState>({ scopeId: conversationId, exchanges: [] })
  const pendingRef = useRef(false)
  const nextId = useRef(1)

  // Derive the visible exchanges: if the scope has changed, treat as empty.
  // This is a pure derivation — no effect needed.
  const exchanges = state.scopeId === conversationId ? state.exchanges : []

  const pending = exchanges.some((e) => e.status === 'pending')

  const send = useCallback(
    (prompt: string) => {
      const trimmed = prompt.trim()
      if (!trimmed || pendingRef.current) return
      pendingRef.current = true

      const id = nextId.current++
      setState((prev) => ({
        scopeId: conversationId,
        exchanges: [
          ...(prev.scopeId === conversationId ? prev.exchanges : []),
          { id, prompt: trimmed, status: 'pending' },
        ],
      }))

      const settle = (update: Partial<Exchange>) => {
        pendingRef.current = false
        setState((prev) => ({
          scopeId: conversationId,
          exchanges: prev.exchanges.map((e) => (e.id === id ? { ...e, ...update } : e)),
        }))
      }

      void post(trimmed, mode, conversationId).then(
        (result) =>
          settle(
            result.kind === 'ok'
              ? { status: 'done', response: result.response }
              : { status: 'error', error: result.message },
          ),
        // A custom PostFn may reject; don't strand pendingRef (locks the composer).
        (err: unknown) =>
          settle({
            status: 'error',
            error: err instanceof Error ? err.message : 'Unexpected error — please try again.',
          }),
      )
    },
    [post, mode, conversationId],
  )

  return { exchanges, send, pending }
}
