/**
 * documentApi -- the Workbench's read calls: one document, its version history
 * and a library query (agent-forge-harness-1kg.6.3, section 4 of its brief).
 *
 * Same conventions as `shell/campaignApi.ts`, whose private `send`/`bodyOf`
 * pattern this copies rather than exports:
 * - Every call sends the session cookie and nothing else that identifies anyone.
 * - An id is checked against `OPAQUE_ID` before it enters a path (SEC-4); a
 *   malformed one makes no request and reads as `unavailable`.
 * - No title, field text or search ever rides in a URL (X-7). The library query
 *   is a POST BODY even with no search; the history cursor is the server's own
 *   opaque value.
 * - A function never throws for an HTTP answer or a network failure, and never
 *   returns a server `message`: callers map the kind to their own copy.
 * - A 401 is the centralized sign-out (`notifyUnauthorized`). A 403 or a 404 on
 *   one resource is `unavailable` -- the client never tells a missing document
 *   from a foreign one (CANVAS-31).
 * - A body that is not the shape asked for is `failed`; a document this client
 *   cannot read because it is newer (a newer schema or an unknown type, X-8) is
 *   `unsupported`, which is not an outage and is never retried.
 *
 * Nothing logs. Two calls write, both from the Campaign Library (1kg.6.4): create
 * (LIB-12) and unarchive (LIB-16's Undo and the Archived filter's Restore). Neither
 * puts a title in a URL, and neither is retried by this module: the caller owns the
 * retry, and a create's `command_id` makes that retry safe.
 */

import { notifyUnauthorized } from '../api'
import { isOpaqueId } from '../shell/workspaceFragment'
import { HISTORY_PAGE_SIZE } from './canvasStatus'
import {
  DOC_TYPE_VERSION,
  DocumentCreateRequestSchema,
  DocumentHistoryPageSchema,
  ErrorBodySchema,
  LibraryPageSchema,
  LibraryQuerySchema,
  parseDocument,
  type Document,
  type DocumentTypeId,
  type DocumentHistoryPage,
  type LibraryPage,
  type LibraryQuery,
} from './contracts'

const UNAUTHORIZED = 401
const FORBIDDEN = 403
const NOT_FOUND = 404
const CONFLICT = 409
const UNPROCESSABLE = 422
const TOO_MANY_REQUESTS = 429

export type DocumentReadResult =
  | { readonly kind: 'ok'; readonly document: Document }
  /** A newer schema version or an unknown type (X-8): a placeholder, not an outage. */
  | { readonly kind: 'unsupported' }
  /** 403, 404 or a malformed id: one state, whichever it was. */
  | { readonly kind: 'unavailable' }
  /** 5xx, a network failure, an unreadable or mismatched body: worth a retry. */
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type HistoryReadResult =
  | { readonly kind: 'ok'; readonly page: DocumentHistoryPage }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type LibraryReadResult =
  | { readonly kind: 'ok'; readonly page: LibraryPage }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type CreateResult =
  | { readonly kind: 'ok'; readonly document: Document }
  /** 409 `account_limit_reached`: the storage cap. Not retryable. */
  | { readonly kind: 'limit' }
  /** 409 `document_unsupported`: the server cannot render what it would store. Not retryable. */
  | { readonly kind: 'unsupported' }
  /** 429: the write throttle. Worth a retry after a wait. */
  | { readonly kind: 'throttled' }
  /** A request the contract refuses (checked here first) or a 422. Not retryable. */
  | { readonly kind: 'invalid' }
  /** 403, 404 or a malformed campaign id. */
  | { readonly kind: 'unavailable' }
  /** 5xx, a network failure, an unreadable or mismatched body: worth a retry with the same command id. */
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type LifecycleResult =
  | { readonly kind: 'ok' }
  | { readonly kind: 'throttled' }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

async function send(fetchImpl: typeof fetch, path: string, init?: RequestInit): Promise<Response | null> {
  try {
    return await fetchImpl(path, { ...init, credentials: 'include' })
  } catch {
    return null
  }
}

async function bodyOf(res: Response): Promise<unknown> {
  try {
    return await res.json()
  } catch {
    return undefined
  }
}

function documentPath(campaignId: string, documentId: string): string {
  return `/campaigns/${encodeURIComponent(campaignId)}/documents/${encodeURIComponent(documentId)}`
}

/** `GET /campaigns/{cid}/documents/{did}`. The answer must name the campaign and
 * document asked for, so a body that belongs elsewhere is never shown (LIB-25). */
export async function getDocument(
  campaignId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DocumentReadResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, documentPath(campaignId, documentId))
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (!res.ok) return { kind: 'failed' }
  const raw = await bodyOf(res)
  if (raw === undefined) return { kind: 'failed' }
  const parsed = parseDocument(raw)
  // A newer schema or an unknown type is a placeholder (X-8); a body that is not a document at all is an outage to retry.
  if (parsed.kind === 'unknown') return { kind: parsed.reason === 'invalid' ? 'failed' : 'unsupported' }
  const document = parsed.value
  if (document.campaign_id !== campaignId || document.document_id !== documentId) return { kind: 'failed' }
  return { kind: 'ok', document }
}

/** `GET /campaigns/{cid}/documents/{did}/versions?limit=20[&cursor=…]`, newest first. */
export async function getDocumentHistory(
  campaignId: string,
  documentId: string,
  cursor: string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<HistoryReadResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  const query = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE) })
  if (cursor !== null) query.set('cursor', cursor)
  const res = await send(fetchImpl, `${documentPath(campaignId, documentId)}/versions?${query.toString()}`)
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (!res.ok) return { kind: 'failed' }
  const page = DocumentHistoryPageSchema.safeParse(await bodyOf(res))
  if (!page.success || page.data.document_id !== documentId) return { kind: 'failed' }
  return { kind: 'ok', page: page.data }
}

/** `POST /campaigns/{cid}/library`. The query is validated first, so one the
 * server would refuse makes no request, and it travels as a JSON body, never a
 * query string (X-7). The caller checks the page's echoed campaign and category
 * against what it asked for (LIB-25). */
export async function queryLibrary(
  query: LibraryQuery,
  fetchImpl: typeof fetch = fetch,
): Promise<LibraryReadResult> {
  const request = LibraryQuerySchema.safeParse(query)
  if (!request.success) return { kind: 'failed' }
  const campaignId = request.data.campaign_id
  if (!isOpaqueId(campaignId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, `/campaigns/${encodeURIComponent(campaignId)}/library`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request.data),
  })
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (!res.ok) return { kind: 'failed' }
  const page = LibraryPageSchema.safeParse(await bodyOf(res))
  return page.success ? { kind: 'ok', page: page.data } : { kind: 'failed' }
}

/** The code an error answer carries (`detail.code`), or null when the body is not the Workbench envelope. */
async function errorCode(res: Response): Promise<string | null> {
  const body = ErrorBodySchema.safeParse(await bodyOf(res))
  return body.success ? body.data.detail.code : null
}

export interface CreateRequestInput {
  /** One id per intent: a retry sends the same one, so it opens the document already made. */
  readonly commandId: string
  readonly type: DocumentTypeId
  readonly name: string
}

/** `POST /campaigns/{cid}/documents` (LIB-12): a document of `type` with only its name, `201` with the
 * stored document. The request is validated first, so one the server would refuse (a stat block with no
 * AC or HP, a blank name) makes no request. The answer must be the campaign and type asked for. */
export async function createDocument(
  campaignId: string,
  input: CreateRequestInput,
  fetchImpl: typeof fetch = fetch,
): Promise<CreateResult> {
  if (!isOpaqueId(campaignId)) return { kind: 'unavailable' }
  const request = DocumentCreateRequestSchema.safeParse({
    schema_version: 1,
    command_id: input.commandId,
    campaign_id: campaignId,
    type: input.type,
    type_version: DOC_TYPE_VERSION[input.type],
    data: { name: input.name },
  })
  if (!request.success) return { kind: 'invalid' }
  const res = await send(fetchImpl, `/campaigns/${encodeURIComponent(campaignId)}/documents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request.data),
  })
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (res.status === TOO_MANY_REQUESTS) return { kind: 'throttled' }
  if (res.status === UNPROCESSABLE) return { kind: 'invalid' }
  if (res.status === CONFLICT) {
    const code = await errorCode(res)
    if (code === 'account_limit_reached') return { kind: 'limit' }
    return code === 'document_unsupported' ? { kind: 'unsupported' } : { kind: 'failed' }
  }
  if (!res.ok) return { kind: 'failed' }
  const raw = await bodyOf(res)
  if (raw === undefined) return { kind: 'failed' }
  const parsed = parseDocument(raw)
  if (parsed.kind === 'unknown') return { kind: 'failed' }
  const document = parsed.value
  if (document.campaign_id !== campaignId || document.type !== input.type) return { kind: 'failed' }
  return { kind: 'ok', document }
}

/** `POST /campaigns/{cid}/documents/{did}/unarchive`: `204`, no body in either direction and no
 * Content-Type. Idempotent on the server, so a retry sends the same call. */
export async function unarchiveDocument(
  campaignId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<LifecycleResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, `${documentPath(campaignId, documentId)}/unarchive`, { method: 'POST' })
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (res.status === TOO_MANY_REQUESTS) return { kind: 'throttled' }
  return res.ok ? { kind: 'ok' } : { kind: 'failed' }
}
