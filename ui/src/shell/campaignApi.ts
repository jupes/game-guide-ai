/**
 * campaignApi -- the client's campaign calls and the restore-time conversation
 * read (agent-forge-harness-1kg.2.5, PR-1a; brief section 7.2).
 *
 * Every call sends the session cookie and nothing else that identifies anyone.
 * A path id is checked against `OPAQUE_ID` before it enters a path (SEC-4) and
 * a malformed one makes no request; no query string ever carries a name, a
 * title or search text (X-7) -- only the server's opaque cursor. A JSON body is
 * always sent as `application/json`: the origin check (SEC-7) refuses anything
 * else with a 403 that reads like a role refusal.
 *
 * Each function returns a discriminated result and never throws for an HTTP
 * answer. A server `message` is never returned: callers map the kind to their
 * own copy. A 401 is the centralized sign-out (`notifyUnauthorized`). A 403 or
 * a 404 on one resource is `unavailable` -- the client never tells a missing
 * campaign from a foreign or an archived one (SEC-3, CANVAS-31) -- while the
 * same refusal on a LIST is an outage to this client (`failed`).
 */

import { z } from 'zod'
import { notifyUnauthorized } from '../api'
import {
  CAMPAIGN_PAGE_MAX_ITEMS,
  CampaignCreateRequestSchema,
  CampaignPageSchema,
  CampaignSchema,
  CONTRACT_VERSION,
  parseConversation,
  type Campaign,
  type Conversation,
} from '../gm/contracts'
import { isOpaqueId } from './workspaceFragment'

const UNAUTHORIZED = 401
const FORBIDDEN = 403
const NOT_FOUND = 404
const UNPROCESSABLE = 422

export type CampaignPageResult =
  /** `dropped` counts the items this client could not read (a newer server);
   * they are left out, never allowed to empty the page. Only `nextCursor ===
   * null` is the end of the list: a short page is not. */
  | { readonly kind: 'ok'; readonly items: readonly Campaign[]; readonly nextCursor: string | null; readonly dropped: number }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type CampaignReadResult =
  | { readonly kind: 'ok'; readonly campaign: Campaign }
  /** 403, 404, or an archived campaign: one state, whichever it was. */
  | { readonly kind: 'unavailable' }
  /** 5xx, a network failure, an unreadable body: worth a retry. */
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type CampaignCreateResult =
  | { readonly kind: 'created'; readonly campaign: Campaign }
  /** A client-invalid name (no request was made) or the server's 422. */
  | { readonly kind: 'invalid' }
  /** Anything else. Never retried here: a create has no idempotency key, so a
   * retry could make a second campaign (only an explicit press may). */
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type ConversationReadResult =
  | { readonly kind: 'ok'; readonly conversation: Conversation }
  /** 403 or 404: missing and foreign read alike. */
  | { readonly kind: 'gone' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

/** The page envelope read strictly; its items read one by one below. */
const CampaignEnvelopeSchema = CampaignPageSchema.extend({
  items: z.array(z.unknown()).max(CAMPAIGN_PAGE_MAX_ITEMS),
})

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

function sendJson(fetchImpl: typeof fetch, path: string, method: string, body: unknown): Promise<Response | null> {
  return send(fetchImpl, path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

/** `GET /campaigns[?cursor=…]`: newest first, archived campaigns left out (the
 * server's default), concluded ones included. */
export async function listCampaigns(
  cursor: string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<CampaignPageResult> {
  const query = cursor === null ? '' : `?cursor=${encodeURIComponent(cursor)}`
  const res = await send(fetchImpl, `/campaigns${query}`)
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (!res.ok) return { kind: 'failed' }
  const envelope = CampaignEnvelopeSchema.safeParse(await bodyOf(res))
  if (!envelope.success) return { kind: 'failed' }
  const items: Campaign[] = []
  for (const raw of envelope.data.items) {
    const item = CampaignSchema.safeParse(raw)
    if (item.success) items.push(item.data)
  }
  return {
    kind: 'ok',
    items,
    nextCursor: envelope.data.next_cursor,
    dropped: envelope.data.items.length - items.length,
  }
}

/** `GET /campaigns/{id}`: the server's say on a campaign id from an untrusted
 * source (a fragment, a stale list). An archived campaign is `unavailable`. */
export async function getCampaign(
  campaignId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<CampaignReadResult> {
  if (!isOpaqueId(campaignId)) return { kind: 'unavailable' }
  const res = await send(fetchImpl, `/campaigns/${encodeURIComponent(campaignId)}`)
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'unavailable' }
  if (!res.ok) return { kind: 'failed' }
  const campaign = CampaignSchema.safeParse(await bodyOf(res))
  if (!campaign.success) return { kind: 'failed' }
  if (campaign.data.archived_at !== null) return { kind: 'unavailable' }
  return { kind: 'ok', campaign: campaign.data }
}

/** `POST /campaigns`. The request is validated first, so a name the server
 * would refuse makes no request. Made exactly once per call. */
export async function createCampaign(
  name: string,
  tone?: string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<CampaignCreateResult> {
  const request = CampaignCreateRequestSchema.safeParse(
    tone === undefined
      ? { schema_version: CONTRACT_VERSION, name }
      : { schema_version: CONTRACT_VERSION, name, tone },
  )
  if (!request.success) return { kind: 'invalid' }
  const res = await sendJson(fetchImpl, '/campaigns', 'POST', request.data)
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === UNPROCESSABLE) return { kind: 'invalid' }
  if (!res.ok) return { kind: 'failed' }
  const campaign = CampaignSchema.safeParse(await bodyOf(res))
  return campaign.success ? { kind: 'created', campaign: campaign.data } : { kind: 'failed' }
}

/** `GET /conversations/{id}`, for the restore check only (I-6): it never
 * claims, unlike the legacy `/messages` read, which must never be called for a
 * campaign thread. The caller decides whether the row belongs where the
 * fragment says it does. */
export async function getConversation(
  conversationId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ConversationReadResult> {
  if (!isOpaqueId(conversationId)) return { kind: 'gone' }
  const res = await send(fetchImpl, `/conversations/${encodeURIComponent(conversationId)}`)
  if (res === null) return { kind: 'failed' }
  if (res.status === UNAUTHORIZED) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  if (res.status === FORBIDDEN || res.status === NOT_FOUND) return { kind: 'gone' }
  if (!res.ok) return { kind: 'failed' }
  const conversation = parseConversation(await bodyOf(res))
  return conversation.kind === 'ok' ? { kind: 'ok', conversation: conversation.value } : { kind: 'failed' }
}
