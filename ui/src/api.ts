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
  | { kind: 'error'; message: string }

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

  const body = (await res.json()) as { messages: StoredMessage[] }
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
 * upload endpoint accepts a JSON body, matching postChat's pattern). */
async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
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

  const body = (await res.json()) as { attachment: Attachment }
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

  const body = (await res.json()) as { attachments: Attachment[] }
  return { kind: 'ok', attachments: body.attachments }
}
