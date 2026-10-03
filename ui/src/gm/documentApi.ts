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
 * Nothing logs. Four calls write for the Library (1kg.6.4): create (LIB-12), archive and unarchive
 * (LIB-16's row action, Undo and Restore) and delete (LIB-18, behind the password, SEC-40); none puts a
 * title or the password in a URL, and the caller owns the retry (a create's `command_id` makes that safe).
 * The seal (1kg.7.3) also writes, and changes no text.
 */

import { notifyUnauthorized } from '../api'
import { isOpaqueId } from '../shell/workspaceFragment'
import { HISTORY_PAGE_SIZE } from './canvasStatus'
import {
  CharacterSheetLinkSchema,
  CONTRACT_VERSION,
  DOC_TYPE_VERSION,
  DOCUMENT_TYPE_IDS,
  DocumentCreateRequestSchema,
  DocumentDeleteRequestSchema,
  DocumentHistoryPageSchema,
  DocumentVersionSnapshotSchema,
  ErrorBodySchema,
  LibraryPageSchema,
  LibraryQuerySchema,
  parseDocument,
  type CharacterSheetLink,
  type Document,
  type DocumentTypeId,
  type DocumentHistoryPage,
  type DocumentVersionSnapshot,
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

export type VersionReadResult =
  | { readonly kind: 'ok'; readonly snapshot: DocumentVersionSnapshot }
  /** A newer schema or a type this client does not know (X-8): never shown as another type. */
  | { readonly kind: 'unsupported' }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type LinkReadResult =
  | { readonly kind: 'ok'; readonly link: CharacterSheetLink }
  | { readonly kind: 'unavailable' }
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

/** `delete` answers: the lifecycle kinds, plus the two refusals a password and a state can make. */
export type DeleteResult =
  | LifecycleResult
  /** 403 `reauth_failed`: the password did not match. Retryable by typing it again (SEC-40). */
  | { readonly kind: 'reauth_failed' }
  /** 409 `document_not_archived`: it was restored (or never archived), so nothing was deleted. */
  | { readonly kind: 'not_archived' }

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

/** Reads a document answer. The body must name the campaign and document asked for,
 * so one that belongs elsewhere is never shown (LIB-25). */
async function readDocumentAnswer(res: Response | null, campaignId: string, documentId: string): Promise<DocumentReadResult> {
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

/** `GET /campaigns/{cid}/documents/{did}`. The answer must name the campaign and
 * document asked for, so a body that belongs elsewhere is never shown (LIB-25). */
export async function getDocument(
  campaignId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DocumentReadResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  return readDocumentAnswer(await send(fetchImpl, documentPath(campaignId, documentId)), campaignId, documentId)
}

/**
 * `POST /campaigns/{cid}/documents/{did}/seal` (1kg.7.3): seal the current text as a
 * version a reveal can pin (CANVAS-34). Idempotent on the server, which reads no body;
 * `{}` is sent as JSON only so the origin check (SEC-7) sees a well-formed request.
 * The answer is the document, and its `version` is the sealed one.
 */
export async function sealDocument(
  campaignId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DocumentReadResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, `${documentPath(campaignId, documentId)}/seal`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  return readDocumentAnswer(res, campaignId, documentId)
}

function namesAnotherEra(raw: unknown): boolean {
  if (typeof raw !== 'object' || raw === null) return false
  const { schema_version: version, type, type_version: typeVersion } = raw as {
    schema_version?: unknown
    type?: unknown
    type_version?: unknown
  }
  const newer = typeof version === 'number' && version > CONTRACT_VERSION
  const known = typeof type === 'string' && (DOCUMENT_TYPE_IDS as readonly string[]).includes(type)
  const newerType = known && typeof typeVersion === 'number' && typeVersion > DOC_TYPE_VERSION[type as DocumentTypeId]
  return newer || (typeof type === 'string' && !known) || newerType
}

/** `GET /campaigns/{cid}/documents/{did}/versions/{n}` (1kg.7.3): the text of one version, which a live reveal is pinned to (REVEAL-8). */
export async function getDocumentVersion(
  campaignId: string,
  documentId: string,
  number: number,
  fetchImpl: typeof fetch = fetch,
): Promise<VersionReadResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId) || !Number.isInteger(number) || number < 1) {
    return { kind: 'unavailable' }
  }
  const res = await send(fetchImpl, `${documentPath(campaignId, documentId)}/versions/${number}`)
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (!res.ok) return { kind: 'failed' }
  const raw = await bodyOf(res)
  const snapshot = DocumentVersionSnapshotSchema.safeParse(raw)
  if (!snapshot.success) return { kind: namesAnotherEra(raw) ? 'unsupported' : 'failed' }
  if (snapshot.data.document_id !== documentId) return { kind: 'failed' }
  return { kind: 'ok', snapshot: snapshot.data }
}

/** `GET /campaigns/{cid}/documents/{did}/link` (1kg.7.3): the seat a character sheet is linked to, ids only. */
export async function getCharacterSheetLink(
  campaignId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<LinkReadResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, `${documentPath(campaignId, documentId)}/link`)
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (!res.ok) return { kind: 'failed' }
  const link = CharacterSheetLinkSchema.safeParse(await bodyOf(res))
  if (!link.success || link.data.document_id !== documentId) return { kind: 'failed' }
  return { kind: 'ok', link: link.data }
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
  /** Other fields a type needs to be valid at birth: a stat block's `ac` and `hp` (LIB-12). */
  readonly fields?: Readonly<Record<string, number>>
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
    data: { name: input.name, ...input.fields },
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

/** `POST /campaigns/{cid}/documents/{did}/archive` (LIB-16, LIB-17): `204`, no body in either direction. A live
 * document is stopped and archived in one transaction by the server, so the caller sends nothing extra. */
export async function archiveDocument(
  campaignId: string,
  documentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<LifecycleResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, `${documentPath(campaignId, documentId)}/archive`, { method: 'POST' })
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (res.status === TOO_MANY_REQUESTS) return { kind: 'throttled' }
  return res.ok ? { kind: 'ok' } : { kind: 'failed' }
}

/**
 * `POST /campaigns/{cid}/documents/{did}/delete` (LIB-18, SEC-40): the whole document and its history, behind
 * the password, in the JSON body (there is no `DELETE` method). The password is never logged, echoed, stored
 * or put in a URL; it leaves this function only in that body. Only an archived document can be deleted.
 */
export async function deleteDocument(
  campaignId: string,
  documentId: string,
  password: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DeleteResult> {
  if (!isOpaqueId(campaignId) || !isOpaqueId(documentId)) return { kind: 'unavailable' }
  const request = DocumentDeleteRequestSchema.safeParse({ schema_version: CONTRACT_VERSION, password })
  // A password the contract refuses cannot be the right one, and no request is made for it.
  if (!request.success) return { kind: 'reauth_failed' }
  const res = await send(fetchImpl, `${documentPath(campaignId, documentId)}/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: CONTRACT_VERSION, password }),
  })
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN) return (await errorCode(res)) === 'reauth_failed' ? { kind: 'reauth_failed' } : { kind: 'unavailable' }
  if (res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (res.status === TOO_MANY_REQUESTS) return { kind: 'throttled' }
  if (res.status === CONFLICT) return (await errorCode(res)) === 'document_not_archived' ? { kind: 'not_archived' } : { kind: 'failed' }
  return res.ok ? { kind: 'ok' } : { kind: 'failed' }
}
