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
 * Nothing logs. The one write is the seal (1kg.7.3), which changes no text.
 */

import { notifyUnauthorized } from '../api'
import { isOpaqueId } from '../shell/workspaceFragment'
import { HISTORY_PAGE_SIZE } from './canvasStatus'
import {
  CharacterSheetLinkSchema,
  CONTRACT_VERSION,
  DOC_TYPE_VERSION,
  DOCUMENT_TYPE_IDS,
  type DocumentTypeId,
  DocumentHistoryPageSchema,
  DocumentVersionSnapshotSchema,
  LibraryPageSchema,
  LibraryQuerySchema,
  parseDocument,
  type CharacterSheetLink,
  type Document,
  type DocumentHistoryPage,
  type DocumentVersionSnapshot,
  type LibraryPage,
  type LibraryQuery,
} from './contracts'

const UNAUTHORIZED = 401
const FORBIDDEN = 403
const NOT_FOUND = 404

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
