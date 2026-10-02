/**
 * revealApi -- the GM's reveal calls (agent-forge-harness-1kg.7.3, brief 3.1) on
 * 1kg.7.2's `/campaigns/{campaign_id}/reveals` routes.
 *
 * Same conventions as `shell/tableSessionApi.ts`: the cookie only; an id is checked
 * against `OPAQUE_ID` before it enters a path (SEC-4), so a malformed one makes no
 * request; JSON always as `application/json` (SEC-7); a function never throws for an
 * HTTP answer or a network failure; a server `message` is never returned; a 401 is
 * the centralized sign-out.
 *
 * Every body is built through its zod request schema first. A request the contract
 * refuses is never sent, so a mask naming `all`, an empty mask or a participant list
 * of nobody cannot leave this module. The client sends KEYS only: no title, alias or
 * field text is a parameter here, so none can ride in a URL or a body (X-7).
 *
 * A non-2xx answer to a Confirm is read as the Workbench `ErrorBody`
 * (`{detail:{code,message,retryable,field,keys}}`), not as FastAPI's validation list
 * (Critic 1): `detail.field` says what the 422 is about. Nothing is retried here;
 * only the Stop courier (`shell/revealStop.ts`) retries.
 */

import { notifyUnauthorized } from '../api'
import { isOpaqueId } from '../shell/workspaceFragment'
import {
  CONTRACT_VERSION,
  ErrorBodySchema,
  RevealAnswerSchema,
  RevealRequestSchema,
  RevealStopRequestSchema,
  type RevealRequest,
  type RevealState,
} from './contracts'

export type RevealReadResult =
  | { readonly kind: 'ok'; readonly state: RevealState | null }
  /** 403, 404 or a malformed id: not this GM's scope, or gone. */
  | { readonly kind: 'unavailable' }
  /** 5xx, a network failure or a body that is not a picture. Never read as "nothing revealed" (REVEAL-13). */
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type ConfirmResult =
  | { readonly kind: 'ok'; readonly state: RevealState | null }
  /** 409: the reveal epoch moved (REVEAL-15). Never retried automatically (REVEAL-22). */
  | { readonly kind: 'conflict' }
  /** 422 on the mask: the keys at fault. Keys only, never their text. */
  | { readonly kind: 'mask_refused'; readonly keys: readonly string[] }
  /** 422 on anything else; `field` is what the server named, or `null`. */
  | { readonly kind: 'refused'; readonly field: 'audience' | 'document_id' | 'version' | null }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'throttled'; readonly retryAfterS: number | null }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }
  /** Not a server answer: a Stop is unacknowledged, so a widening is not sent (REVEAL-22). */
  | { readonly kind: 'blocked' }

export type StopResult =
  | { readonly kind: 'ok'; readonly state: RevealState | null }
  /** 404 (or 403): nothing to stop here any more. */
  | { readonly kind: 'gone' }
  | { readonly kind: 'throttled'; readonly retryAfterS: number | null }
  /** 422, or a request the contract refuses: retrying cannot help. */
  | { readonly kind: 'invalid' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type StopScope =
  | { readonly commandId: string; readonly scope: 'document'; readonly documentId: string }
  | { readonly commandId: string; readonly scope: 'all' }

/** What the client holds of a Confirm: everything but the contract's own version. */
export type ConfirmRequest = Omit<RevealRequest, 'schema_version'>

const JSON_HEADERS = { 'Content-Type': 'application/json' }

function revealsPath(campaignId: string, tail = ''): string {
  return `/campaigns/${encodeURIComponent(campaignId)}/reveals${tail}`
}

async function send(fetchImpl: typeof fetch, path: string, init: RequestInit): Promise<Response | null> {
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

/** The picture in a 2xx answer, or `undefined` when the body is not one. */
async function pictureOf(res: Response): Promise<{ readonly state: RevealState | null } | undefined> {
  const answer = RevealAnswerSchema.safeParse(await bodyOf(res))
  return answer.success ? { state: answer.data.state } : undefined
}

/** A 429's `retry_after_s`, when the server's error body carries one. */
async function retryAfterOf(res: Response): Promise<number | null> {
  const body = ErrorBodySchema.safeParse(await bodyOf(res))
  return body.success ? (body.data.detail.retry_after_s ?? null) : null
}

const REFUSED_FIELDS = ['audience', 'document_id', 'version'] as const

/** `GET /campaigns/{id}/reveals`: the GM's picture, or `state: null` for no live session. */
export async function readReveals(campaignId: string, fetchImpl: typeof fetch = fetch): Promise<RevealReadResult> {
  if (!isOpaqueId(campaignId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, revealsPath(campaignId), {})
  if (res === null) return { kind: 'failed' }
  if (res.status === 401) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === 403 || res.status === 404) return { kind: 'unavailable' }
  if (!res.ok) return { kind: 'failed' }
  const picture = await pictureOf(res)
  return picture === undefined ? { kind: 'failed' } : { kind: 'ok', state: picture.state }
}

/** `POST /campaigns/{id}/reveals`: one Confirm, sent once. Replay-safe by command id, never auto-retried. */
export async function confirmReveal(
  campaignId: string,
  request: ConfirmRequest,
  fetchImpl: typeof fetch = fetch,
): Promise<ConfirmResult> {
  if (!isOpaqueId(campaignId)) return { kind: 'unavailable' }
  const body = RevealRequestSchema.safeParse({ schema_version: CONTRACT_VERSION, ...request })
  if (!body.success) return { kind: 'failed' }
  const res = await send(fetchImpl, revealsPath(campaignId), {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(body.data),
  })
  if (res === null) return { kind: 'failed' }
  if (res.status === 401) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.ok) {
    const picture = await pictureOf(res)
    return picture === undefined ? { kind: 'failed' } : { kind: 'ok', state: picture.state }
  }
  if (res.status === 409) return { kind: 'conflict' }
  if (res.status === 403 || res.status === 404) return { kind: 'unavailable' }
  if (res.status === 429) return { kind: 'throttled', retryAfterS: await retryAfterOf(res) }
  if (res.status === 422) {
    const error = ErrorBodySchema.safeParse(await bodyOf(res))
    const detail = error.success ? error.data.detail : null
    if (detail?.field === 'mask') return { kind: 'mask_refused', keys: detail.keys ?? [] }
    const field = REFUSED_FIELDS.find((name) => name === detail?.field) ?? null
    return { kind: 'refused', field }
  }
  return { kind: 'failed' }
}

/**
 * `POST /campaigns/{id}/reveals/stop`: names a document or `all`, and never an epoch
 * (X-3: a narrowing is never stale). Sent once per call; the courier retries.
 */
export async function stopReveal(campaignId: string, stop: StopScope, fetchImpl: typeof fetch = fetch): Promise<StopResult> {
  if (!isOpaqueId(campaignId)) return { kind: 'invalid' }
  const body = RevealStopRequestSchema.safeParse({
    schema_version: CONTRACT_VERSION,
    command_id: stop.commandId,
    scope: stop.scope,
    ...(stop.scope === 'document' ? { document_id: stop.documentId } : {}),
  })
  if (!body.success || (stop.scope === 'document' && !isOpaqueId(stop.documentId))) return { kind: 'invalid' }
  const res = await send(fetchImpl, revealsPath(campaignId, '/stop'), {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(body.data),
  })
  if (res === null) return { kind: 'failed' }
  if (res.status === 401) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.ok) {
    const picture = await pictureOf(res)
    return picture === undefined ? { kind: 'failed' } : { kind: 'ok', state: picture.state }
  }
  if (res.status === 403 || res.status === 404) return { kind: 'gone' }
  if (res.status === 422) return { kind: 'invalid' }
  if (res.status === 429) return { kind: 'throttled', retryAfterS: await retryAfterOf(res) }
  return { kind: 'failed' }
}
