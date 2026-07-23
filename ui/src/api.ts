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

/** One LLM-invented spell-usage idea (spell mode only). */
export interface Suggestion {
  style: 'practical' | 'roleplay' | 'wacky'
  text: string
}

export interface ChatResponse {
  answer: string
  sources: Source[]
  answerable: boolean
  /** Spell mode only; null/absent elsewhere or when generation failed. */
  suggestions?: Suggestion[] | null
  /** Optional echo fields from the service. */
  mode?: ChatMode
  conversation_id?: string | null
}

export type ChatResult =
  | { kind: 'ok'; response: ChatResponse }
  | {
      kind: 'error'
      message: string
      outcome?: 'http_error' | 'network_error' | 'aborted'
    }

/** One persisted chat turn — mirrors service StoredMessage. */
export interface StoredMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  mode: ChatMode
  created_at: string
  /** Assistant turns from spell mode carry their suggestions. */
  suggestions?: Suggestion[] | null
}

export type MessagesResult =
  | { kind: 'ok'; messages: StoredMessage[] }
  | { kind: 'error'; message: string }

/** Parse a response body as JSON, or null when it isn't valid JSON. A proxy
 * misroute can answer 200 with HTML (that was bug cnqf) — and this module's
 * contract is that the UI never throws on a bad day. */
async function parseJson<T>(res: Response): Promise<T | null> {
  try {
    return (await res.json()) as T
  } catch {
    return null
  }
}

const UNREADABLE = 'The service returned an unreadable response.'

/** Recall a conversation's stored history (most recent window, oldest-first). */
export async function getMessages(
  conversationId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MessagesResult> {
  let res: Response
  try {
    res = await fetchImpl(`/conversations/${encodeURIComponent(conversationId)}/messages`)
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }

  if (!res.ok) {
    return { kind: 'error', message: `Message history unavailable (${res.status}).` }
  }

  const body = await parseJson<{ messages: StoredMessage[] }>(res)
  if (body === null) return { kind: 'error', message: UNREADABLE }
  return { kind: 'ok', messages: body.messages }
}

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
    return {
      kind: 'error',
      message: "Couldn't reach the service — is it running? (network error)",
      outcome: 'network_error',
    }
  }

  if (res.status === 422) {
    return {
      kind: 'error',
      message: 'The prompt was rejected — please enter a question.',
      outcome: 'http_error',
    }
  }
  if (res.status === 503) {
    return {
      kind: 'error',
      message: 'Service unavailable (starting up or upstream error) — try again shortly.',
      outcome: 'http_error',
    }
  }
  if (!res.ok) {
    return {
      kind: 'error',
      message: `Unexpected response (${res.status}).`,
      outcome: 'http_error',
    }
  }

  const response = await parseJson<ChatResponse>(res)
  if (response === null) {
    return { kind: 'error', message: UNREADABLE, outcome: 'http_error' }
  }
  return { kind: 'ok', response }
}

// ── File attachments (swe1.6) ─────────────────────────────────────────────────

/** UI-facing attachment metadata — mirrors service Attachment (extracted text
 * stays server-side and is never sent to the client). */
export interface Attachment {
  id: number
  filename: string
  content_type: string
  chars: number
  created_at: string
}

export type UploadAttachmentResult =
  | { kind: 'ok'; attachment: Attachment }
  | { kind: 'error'; message: string }

export type AttachmentsResult =
  | { kind: 'ok'; attachments: Attachment[] }
  | { kind: 'error'; message: string }

/** Read a File's bytes and base64-encode them (no multipart dependency — the
 * upload endpoint accepts a JSON body, matching postChat's pattern). Converted
 * in 32 KiB slices: one string append per byte is painfully slow on MB-sized
 * files, and fromCharCode over a whole 2 MB buffer overflows the argument limit. */
async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const bytes = new Uint8Array(buffer)
  const parts: string[] = []
  for (let i = 0; i < bytes.length; i += 0x8000) {
    parts.push(String.fromCharCode(...bytes.subarray(i, i + 0x8000)))
  }
  return btoa(parts.join(''))
}

/** Upload a file as an attachment to a conversation; its text is extracted
 * server-side and, from then on, grounds answers in that conversation. */
export async function uploadAttachment(
  conversationId: string,
  file: File,
  fetchImpl: typeof fetch = fetch,
): Promise<UploadAttachmentResult> {
  let data: string
  try {
    data = await fileToBase64(file)
  } catch {
    return { kind: 'error', message: "Couldn't read the file — please try again." }
  }

  let res: Response
  try {
    res = await fetchImpl(`/conversations/${encodeURIComponent(conversationId)}/attachments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: file.name, content_type: file.type, data }),
    })
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }

  if (res.status === 415) {
    return { kind: 'error', message: "That file type isn't supported." }
  }
  if (res.status === 413) {
    return { kind: 'error', message: 'That file is too large.' }
  }
  if (res.status === 422) {
    return { kind: 'error', message: 'The attachment was rejected — please try a different file.' }
  }
  if (!res.ok) {
    return { kind: 'error', message: `Unexpected response (${res.status}).` }
  }

  const body = await parseJson<{ attachment: Attachment }>(res)
  if (body === null) return { kind: 'error', message: UNREADABLE }
  return { kind: 'ok', attachment: body.attachment }
}

/** List a conversation's attachments (metadata only). */
export async function getAttachments(
  conversationId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<AttachmentsResult> {
  let res: Response
  try {
    res = await fetchImpl(`/conversations/${encodeURIComponent(conversationId)}/attachments`)
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }

  if (!res.ok) {
    return { kind: 'error', message: `Attachments unavailable (${res.status}).` }
  }

  const body = await parseJson<{ attachments: Attachment[] }>(res)
  if (body === null) return { kind: 'error', message: UNREADABLE }
  return { kind: 'ok', attachments: body.attachments }
}
