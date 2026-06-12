/**
 * Chat state hook — owns the client-side exchange list (the service is
 * stateless). One in-flight request at a time; empty prompts are ignored.
 */

import { useCallback, useRef, useState } from 'react'
import { postChat } from './api'
import type { ChatResponse, ChatResult } from './api'

export type ExchangeStatus = 'pending' | 'done' | 'error'

export interface Exchange {
  id: number
  prompt: string
  status: ExchangeStatus
  response?: ChatResponse
  error?: string
}

type PostFn = (prompt: string) => Promise<ChatResult>

export function useChat(post: PostFn = postChat) {
  const [exchanges, setExchanges] = useState<Exchange[]>([])
  const pendingRef = useRef(false)
  const nextId = useRef(1)

  const pending = exchanges.some((e) => e.status === 'pending')

  const send = useCallback(
    (prompt: string) => {
      const trimmed = prompt.trim()
      if (!trimmed || pendingRef.current) return
      pendingRef.current = true

      const id = nextId.current++
      setExchanges((prev) => [...prev, { id, prompt: trimmed, status: 'pending' }])

      void post(trimmed).then((result) => {
        pendingRef.current = false
        setExchanges((prev) =>
          prev.map((e) => {
            if (e.id !== id) return e
            return result.kind === 'ok'
              ? { ...e, status: 'done', response: result.response }
              : { ...e, status: 'error', error: result.message }
          }),
        )
      })
    },
    [post],
  )

  return { exchanges, send, pending }
}
