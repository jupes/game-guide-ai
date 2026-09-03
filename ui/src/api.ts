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
      outcome?: 'http_error' | 'network_error' | 'aborted' | 'throttled' | 'conversation_mismatch'
      /** Whether a retry might succeed (D4). Absent when not applicable (network
       * errors, aborts) or unknown (legacy/unstructured error bodies). */
      retryable?: boolean
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

// ── D4 normalized error/status mapping (b8o.2) ───────────────────────────────
// service/app.py's 4xx/5xx errors for the LLM-error categories carry a
// structured body — {detail: {category, retryable, message}} — instead of
// the plain-string `detail` FastAPI's own Pydantic validation errors use, so
// this client can tell them apart and switch on `category` rather than
// parsing English text or falling back to "Unexpected response (<status>)".

interface StructuredErrorDetail {
  category: string
  retryable: boolean
  message: string
}

function structuredErrorDetail(body: unknown): StructuredErrorDetail | null {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return null
  const detail = (body as { detail: unknown }).detail
  if (typeof detail !== 'object' || detail === null) return null
  const d = detail as Record<string, unknown>
  if (typeof d.category !== 'string' || typeof d.retryable !== 'boolean' || typeof d.message !== 'string') {
    return null
  }
  return { category: d.category, retryable: d.retryable, message: d.message }
}

/** Human-facing message per category — distinct from the backend's own
 * `message` field, which is written for logs/operators, not testers. */
const CATEGORY_MESSAGE: Record<string, string> = {
  rate_limit: 'The model provider is busy — try again shortly.',
  content_filter: "That request was refused by the model provider's content policy.",
  invalid_request: "That request couldn't be processed — please rephrase and try again.",
  authentication: 'The model provider is temporarily unavailable — we have been notified.',
  quota: 'The model provider is temporarily unavailable — we have been notified.',
  timeout: 'The model provider timed out — try again in a moment.',
  upstream_unavailable: 'The model provider is temporarily unavailable — try again in a moment.',
  unknown: 'An unexpected upstream error occurred — try again in a moment.',
}

const UNREADABLE = 'The service returned an unreadable response.'

// ── Centralized 401 handling (x5bz.2) ────────────────────────────────────────
// Any guarded call that comes back 401 means the session is gone (expired,
// revoked, account deleted). Rather than each caller inventing its own
// recovery, they all report it here and the auth provider flips the app back
// to the Login screen. Deliberately NOT fired for /auth/login or /auth/me,
// where a 401 is a normal answer rather than a lost session.

type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

function notifyUnauthorized(): void {
  unauthorizedHandler?.()
}

/** Recall a conversation's stored history (most recent window, oldest-first). */
export async function getMessages(
  conversationId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MessagesResult> {
  let res: Response
  try {
    res = await fetchImpl(
      `/conversations/${encodeURIComponent(conversationId)}/messages`,
      { credentials: 'include' },
    )
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }

  if (res.status === 401) notifyUnauthorized()
  if (!res.ok) {
    return { kind: 'error', message: `Message history unavailable (${res.status}).` }
  }

  const body = await parseJson<{ messages: StoredMessage[] }>(res)
  if (body === null) return { kind: 'error', message: UNREADABLE }
  return { kind: 'ok', messages: body.messages }
}

// ── Throttling (x5bz.3) ──────────────────────────────────────────────────────
// Three different things produce a 429 here. Two are the service's own cost
// guard and identify themselves with X-Chat-Throttled; the third is Cloud Run,
// which answers 429 when it has no instance free. Reporting all three as "you
// are going too fast" would blame a tester for the platform's capacity, and
// send them away for a wait that isn't theirs to serve.

const CHAT_THROTTLE_HEADER = 'X-Chat-Throttled'

/** "in 90 seconds" / "in 2 minutes" — a wait a person can act on. */
function waitPhrase(retryAfter: string | null): string {
  const seconds = Number(retryAfter)
  if (!Number.isFinite(seconds) || seconds <= 0) return 'shortly'
  if (seconds < 60) return `in ${Math.ceil(seconds)} seconds`
  const minutes = Math.ceil(seconds / 60)
  return minutes === 1 ? 'in about a minute' : `in about ${minutes} minutes`
}

function throttled(res: Response): ChatResult {
  const reason = res.headers.get(CHAT_THROTTLE_HEADER)
  if (reason === 'daily') {
    return {
      kind: 'error',
      message:
        "The tavern is closed for today — the pilot's daily question limit is spent. " +
        'It resets overnight; your conversations are all still here.',
      outcome: 'throttled',
    }
  }
  if (reason === 'user') {
    return {
      kind: 'error',
      message: `That's a lot of questions at once — try again ${waitPhrase(res.headers.get('Retry-After'))}.`,
      outcome: 'throttled',
    }
  }
  // Unmarked: the platform, not us.
  return {
    kind: 'error',
    message: 'The service is busy right now — try again in a moment.',
    outcome: 'http_error',
  }
}

export async function postChat(
  prompt: string,
  mode: ChatMode = 'sage',
  conversationId?: string | null,
  fetchImpl: typeof fetch = fetch,
  /** "auto" or a specific enabled catalog alias (b8o.2). Kept as the LAST
   * param, after fetchImpl, so no existing positional call site (which all
   * predate this field) needed to change. */
  modelPreference: string = 'auto',
): Promise<ChatResult> {
  let res: Response
  try {
    res = await fetchImpl('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        prompt, mode, conversation_id: conversationId ?? null,
        model_preference: modelPreference,
      }),
    })
  } catch {
    return {
      kind: 'error',
      message: "Couldn't reach the service — is it running? (network error)",
      outcome: 'network_error',
    }
  }

  if (res.status === 401) {
    notifyUnauthorized()
    return {
      kind: 'error',
      message: 'Your session has expired — please sign in again.',
      outcome: 'http_error',
    }
  }
  if (res.status === 403) {
    return {
      kind: 'error',
      message: "You don't have access to that channel or conversation.",
      outcome: 'http_error',
    }
  }
  // 409 (b8o.2, D4): this conversation is bound to a different model
  // preference — the recovery is starting a new conversation, not a retry.
  if (res.status === 409) {
    return {
      kind: 'error',
      message: 'This conversation is locked to a different model — start a new conversation to change it.',
      outcome: 'conversation_mismatch',
      retryable: false,
    }
  }
  if (res.status === 422) {
    const structured = structuredErrorDetail(await parseJson<unknown>(res))
    if (structured !== null) {
      return {
        kind: 'error',
        message: CATEGORY_MESSAGE[structured.category] ?? structured.message,
        outcome: 'http_error',
        retryable: structured.retryable,
      }
    }
    // Legacy shape: FastAPI's own Pydantic-validation 422 (e.g. empty prompt)
    // carries a plain-string/array detail, not {category, retryable, message}.
    return {
      kind: 'error',
      message: 'The prompt was rejected — please enter a question.',
      outcome: 'http_error',
      retryable: false,
    }
  }
  if (res.status === 503) {
    return {
      kind: 'error',
      message: 'Service unavailable (starting up or upstream error) — try again shortly.',
      outcome: 'http_error',
    }
  }
  if (res.status === 502) {
    const structured = structuredErrorDetail(await parseJson<unknown>(res))
    if (structured !== null) {
      return {
        kind: 'error',
        message: CATEGORY_MESSAGE[structured.category] ?? structured.message,
        outcome: 'http_error',
        retryable: structured.retryable,
      }
    }
    return {
      kind: 'error',
      message: 'Unexpected response (502).',
      outcome: 'http_error',
    }
  }
  if (res.status === 429) {
    // Ours (cost guard) always carries X-Chat-Throttled; a provider
    // rate_limit (D4) reaching this far never does — distinct events, and
    // conflating them would blame a tester for the provider being busy.
    if (res.headers.get(CHAT_THROTTLE_HEADER) !== null) return throttled(res)
    const structured = structuredErrorDetail(await parseJson<unknown>(res))
    if (structured !== null) {
      return {
        kind: 'error',
        message: CATEGORY_MESSAGE[structured.category] ?? structured.message,
        outcome: 'http_error',
        retryable: structured.retryable,
      }
    }
    return throttled(res)
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
      credentials: 'include',
      body: JSON.stringify({ filename: file.name, content_type: file.type, data }),
    })
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }

  if (res.status === 401) notifyUnauthorized()
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
    res = await fetchImpl(
      `/conversations/${encodeURIComponent(conversationId)}/attachments`,
      { credentials: 'include' },
    )
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }

  if (res.status === 401) notifyUnauthorized()
  if (!res.ok) {
    return { kind: 'error', message: `Attachments unavailable (${res.status}).` }
  }

  const body = await parseJson<{ attachments: Attachment[] }>(res)
  if (body === null) return { kind: 'error', message: UNREADABLE }
  return { kind: 'ok', attachments: body.attachments }
}

// ── Auth (x5bz.2) — mirrors service.models.AuthUser ──────────────────────────

export interface AuthUser {
  email: string
  role: 'player' | 'dm'
}

export type AuthResult =
  | { kind: 'ok'; user: AuthUser }
  | { kind: 'error'; message: string; status?: number }

/** Turn a FastAPI error body into a displayable string.
 *
 * `detail` is a plain string for our own HTTPExceptions, but for a 422 it is an
 * ARRAY of validation objects — handing that straight to React throws
 * ("objects are not valid as a React child"). Normalize both shapes. */
function errorMessage(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const first = detail.find(
      (d): d is { msg: string } =>
        typeof (d as { msg?: unknown })?.msg === 'string',
    )
    if (first) return first.msg
  }
  return `Request failed (${status}).`
}

async function postAuthJson(
  path: string,
  body: Record<string, string>,
  fetchImpl: typeof fetch,
): Promise<AuthResult> {
  let res: Response
  try {
    res = await fetchImpl(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    })
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }
  if (!res.ok) {
    return {
      kind: 'error',
      status: res.status,
      message: errorMessage(await parseJson<unknown>(res), res.status),
    }
  }
  const parsed = await parseJson<AuthUser>(res)
  if (parsed === null) return { kind: 'error', message: UNREADABLE }
  return { kind: 'ok', user: parsed }
}

/** Redeem a one-time invite to create an account; the service sets the
 * session cookie on success. */
export function signup(
  email: string,
  password: string,
  invite: string,
  fetchImpl: typeof fetch = fetch,
): Promise<AuthResult> {
  return postAuthJson('/auth/signup', { email, password, invite }, fetchImpl)
}

export function login(
  email: string,
  password: string,
  fetchImpl: typeof fetch = fetch,
): Promise<AuthResult> {
  return postAuthJson('/auth/login', { email, password }, fetchImpl)
}

/** Clear the session cookie. Returns whether the SERVER actually cleared it.
 *
 * The cookie is httpOnly, so only a successful server response ends the
 * session — clearing local state on a failed request would show "signed out"
 * while a refresh silently signs the user back in (bad on a shared device). */
export async function logout(fetchImpl: typeof fetch = fetch): Promise<boolean> {
  try {
    const res = await fetchImpl('/auth/logout', { method: 'POST', credentials: 'include' })
    return res.ok
  } catch {
    return false
  }
}

/** Who (if anyone) the current session cookie belongs to.
 *
 * Every failure is an AuthResult error rather than a thrown exception, but the
 * caller MUST distinguish them by `status`: only 401 means "not signed in". A
 * 503 or a network failure means the question went unanswered, and `status` is
 * absent for the latter — see currentUser.tsx, which maps anything that is not
 * a 401 to `unavailable` rather than logging the user out. */
export async function getMe(fetchImpl: typeof fetch = fetch): Promise<AuthResult> {
  let res: Response
  try {
    res = await fetchImpl('/auth/me', { credentials: 'include' })
  } catch {
    return { kind: 'error', message: "Couldn't reach the service — is it running? (network error)" }
  }
  if (!res.ok) {
    return {
      kind: 'error',
      status: res.status,
      // The message follows the status: calling a 503 "not signed in" is how the
      // outage got mistaken for a logout in the first place.
      message: res.status === 401
        ? 'not signed in'
        : `Couldn't check your session (${res.status}).`,
    }
  }
  const parsed = await parseJson<AuthUser>(res)
  if (parsed === null) return { kind: 'error', message: UNREADABLE }
  return { kind: 'ok', user: parsed }
}
