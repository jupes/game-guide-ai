/**
 * Typed client for the agent service — mirrors service/models.py exactly.
 *
 * Refusals are NOT errors: the service answers 200 with answerable=false and a
 * fixed refusal string. Errors (422/503/network) come back as a discriminated
 * result so the UI never throws on a bad day.
 */

export type ChatMode = 'sage' | 'spell' | 'rules' | 'gm'

export interface Source {
  book: string
  chapter: string | null
  section: string | null
  entity: string | null
  page: number | null
  snippet: string
}

export interface ChatResponse {
  answer: string
  sources: Source[]
  answerable: boolean
  /** Optional echo fields from the service. */
  mode?: ChatMode
  conversation_id?: string | null
}

export type ChatResult =
  | { kind: 'ok'; response: ChatResponse }
  | { kind: 'error'; message: string }

export async function postChat(
  prompt: string,
  mode: ChatMode = 'sage',
  conversationId?: string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<ChatResult> {
  let res: Response
  try {
    res = await fetchImpl('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, mode, conversation_id: conversationId ?? null }),
    })
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }

  if (res.status === 422) {
    return { kind: 'error', message: 'The prompt was rejected — please enter a question.' }
  }
  if (res.status === 503) {
    return { kind: 'error', message: 'Service unavailable (starting up or upstream error) — try again shortly.' }
  }
  if (!res.ok) {
    return { kind: 'error', message: `Unexpected response (${res.status}).` }
  }

  const response = (await res.json()) as ChatResponse
  return { kind: 'ok', response }
}
