/**
 * workspaceFragment -- the one URL-fragment grammar for workspace state
 * (CANVAS-30; agent-forge-harness-1kg.2.5, PR-1a).
 *
 * Workspace state rides in the fragment as OPAQUE ids only: a fragment never
 * reaches the server or its logs (X-7), and needs no router or proxy change.
 * A campaign's name, a thread's title and any other GM-private text never
 * enter a URL; neither does a seat or a participant id (SEC-43).
 *
 * The grammar is `inviteToken.ts`'s: raw `name=value` pairs joined by `&`, each
 * pair's NAME decoded by `URLSearchParams` exactly as `scrubReservedFragmentKeys`
 * decodes it, each VALUE read as written (never percent-decoded). This module
 * owns two keys, `campaign` and `conversation`; `1kg.6.3` adds `document` here
 * and owns the module after this bead. `invite` and `token` stay reserved to
 * `inviteToken.ts` and are never written by anything in this file.
 */

import { RESERVED_FRAGMENT_KEYS } from './inviteToken'
import { pathForScreen } from './routes'

/** An opaque id as the wire carries it (`OpaqueIdSchema` in `gm/contracts.ts`,
 * which keeps its own copy private). A path segment and a fragment value are
 * both checked against this before they are used. */
export const OPAQUE_ID = /^[A-Za-z0-9_-]{1,64}$/

export function isOpaqueId(value: string): boolean {
  return OPAQUE_ID.test(value)
}

const CAMPAIGN_KEY = 'campaign'
const CONVERSATION_KEY = 'conversation'
const OWN_KEYS: readonly string[] = [CAMPAIGN_KEY, CONVERSATION_KEY]

/** What the fragment says about the workspace. `conversationId` is non-null only
 * alongside a well-formed `campaignId`: a thread means nothing without its
 * campaign, and the server re-checks both (I-6). */
export interface WorkspaceKeys {
  readonly campaignId: string | null
  readonly conversationId: string | null
}

export const NO_WORKSPACE_KEYS: WorkspaceKeys = { campaignId: null, conversationId: null }

function withoutHash(hash: string): string {
  return hash.startsWith('#') ? hash.slice(1) : hash
}

/** The pairs of a fragment. Empty segments (`a=1&&b=2`, a trailing `&`) carry no
 * pair and are not kept. */
function pairsOf(hash: string): string[] {
  const raw = withoutHash(hash)
  return raw === '' ? [] : raw.split('&').filter((pair) => pair !== '')
}

/** A pair's name, decoded as `scrubReservedFragmentKeys` decodes it
 * (`camp%61ign=x` is `campaign`). */
function nameOf(pair: string): string {
  const first = new URLSearchParams(pair).keys().next()
  return first.done === true ? '' : first.value
}

/** A pair's value, exactly as written: `%41` stays `%41` and is refused. */
function valueOf(pair: string): string {
  const at = pair.indexOf('=')
  return at < 0 ? '' : pair.slice(at + 1)
}

function firstWellFormed(pairs: readonly string[], key: string): string | null {
  for (const pair of pairs) {
    if (nameOf(pair) !== key) continue
    const value = valueOf(pair)
    if (isOpaqueId(value)) return value
  }
  return null
}

/** Read the workspace keys. The first well-formed occurrence of each wins;
 * anything else is absent. */
export function readWorkspaceKeys(hash: string): WorkspaceKeys {
  if (hash.startsWith('?')) return NO_WORKSPACE_KEYS
  const pairs = pairsOf(hash)
  const campaignId = firstWellFormed(pairs, CAMPAIGN_KEY)
  if (campaignId === null) return NO_WORKSPACE_KEYS
  return { campaignId, conversationId: firstWellFormed(pairs, CONVERSATION_KEY) }
}

/** The fragment (without its `#`) that carries `keys` and every foreign pair of
 * `hash`, byte for byte and in order. Every occurrence of an own key is removed
 * -- duplicates and malformed ones included -- and so is every reserved pair
 * (`invite`, `token`), carried over or not: this writer may run before
 * `UrlNavigation`'s boot scrub, and must never write a credential back. The own
 * pairs are appended, `campaign` then `conversation`; an id that is not opaque,
 * or a `conversation` without a `campaign`, is not written. */
export function fragmentWithKeys(hash: string, keys: WorkspaceKeys): string {
  const kept = pairsOf(hash).filter((pair) => {
    const name = nameOf(pair)
    return !OWN_KEYS.includes(name) && !RESERVED_FRAGMENT_KEYS.includes(name)
  })
  const { campaignId, conversationId } = keys
  if (campaignId !== null && isOpaqueId(campaignId)) {
    kept.push(`${CAMPAIGN_KEY}=${campaignId}`)
    if (conversationId !== null && isOpaqueId(conversationId)) {
      kept.push(`${CONVERSATION_KEY}=${conversationId}`)
    }
  }
  return kept.join('&')
}

/** Put `keys` into the address bar with `history.replaceState` -- never
 * `pushState` (CANVAS-30, LAYOUT-11): Back leaves the app rather than stepping
 * through campaigns. Path, query and `history.state` are kept. Writes nothing,
 * and returns false, when the fragment would not change. */
export function replaceWorkspaceKeys(keys: WorkspaceKeys, win: Window = window): boolean {
  const { hash, pathname, search } = win.location
  const next = fragmentWithKeys(hash, keys)
  if (next === withoutHash(hash)) return false
  win.history.replaceState(win.history.state, '', pathname + search + (next === '' ? '' : `#${next}`))
  return true
}

/** A cold load's restore intent: non-null only for the workspace path with a
 * well-formed `campaign` key (I-4). Without one, the router's `coldLoad: 'home'`
 * holds and the page lands on `/` exactly as before this bead. */
export interface CampaignRestore {
  readonly campaignId: string
  readonly conversationId: string | null
}

export function readCampaignRestore(pathname: string, hash: string): CampaignRestore | null {
  if (pathname !== pathForScreen('workspace')) return null
  const { campaignId, conversationId } = readWorkspaceKeys(hash)
  return campaignId === null ? null : { campaignId, conversationId }
}
