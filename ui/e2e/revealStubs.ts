/**
 * revealStubs.ts -- the reveal surface's routes for the e2e stack
 * (agent-forge-harness-1kg.7.3, brief section 9).
 *
 * The e2e stack is the real route table with in-memory auth and NO database, so every
 * `/campaigns...` request answers 503. `workbenchStubs.ts` stubs the Workbench's reads;
 * this file stubs the reveal surface on top of it, and is STATEFUL: a Confirm sets the
 * table slot live, bumps the epoch and answers the new picture, a Stop clears it.
 * `installRevealRoutes` is registered AFTER `installWorkbenchRoutes`, so its handler runs
 * first and `route.fallback()` hands everything else to the Workbench stubs.
 *
 * The bodies are built by pure functions, and `ui/src/gm/revealE2eStubs.test.ts` drives
 * the world through a Confirm and a Stop and parses every answer with the real contract
 * schemas, so this spec cannot pass on a shape the app would reject. Ids are shaped like
 * the server's (a prefix and 22 to 60 base64url characters).
 */

import type { Page, Route } from '@playwright/test'
import { CAMPAIGN_ID, DOCUMENT, DOCUMENT_ID } from './workbenchStubs'

export const SESSION_ID = 'ses_e2eRevealSession00000000001'
export const SEAT_ID = 'par_e2eBrannSeat0000000000001'
export const SEAT_ALIAS = 'Brann'
const DISCLOSURE_PREFIX = 'dis_e2eRevealDisclosure'

const HOUR_MS = 3_600_000

/** A live session, with `started_at` and `ends_at` computed at REQUEST time so it never expires under a slow run. */
export function tableSessionAnswer(now: number = Date.now()): Record<string, unknown> {
  return {
    schema_version: 1,
    session: {
      schema_version: 1,
      session_id: SESSION_ID,
      campaign_id: CAMPAIGN_ID,
      state: 'live',
      gen: 1,
      audio_epoch: 0,
      reveal_epoch: 3,
      started_at: new Date(now - HOUR_MS).toISOString(),
      ends_at: new Date(now + 6 * HOUR_MS).toISOString(),
      ended_at: null,
      audio: false,
      screens: [],
    },
  }
}

/** One confirmed seat. */
export const SEAT_PAGE = {
  schema_version: 1,
  items: [
    {
      schema_version: 1,
      participant_id: SEAT_ID,
      alias: SEAT_ALIAS,
      status: 'confirmed',
      address: null,
      created_at: '2026-09-16T19:20:11Z',
      offered_at: null,
      offer_expires_at: null,
      accepted_at: null,
      confirmed_at: '2026-09-16T19:25:00Z',
      removed_at: null,
    },
  ],
  next_cursor: null,
}

interface LiveEntry {
  disclosure_id: string
  document_id: string
  type: 'npc'
  version: number
  mask: string[]
  stale_text: false
  pending_delivery: false
}

/** What a Confirm sent, as far as the world reads it. */
export interface ConfirmBody {
  document_id: string
  version: number
  mask: string[]
}

/** The reveal picture the stub server holds: one table slot, an epoch and a sequence. */
export class RevealWorld {
  epoch = 3
  private seq = 1
  private live: LiveEntry | null = null
  private disclosures = 0

  picture(): Record<string, unknown> {
    return {
      schema_version: 1,
      state: {
        session_id: SESSION_ID,
        gen: 1,
        reveal_epoch: this.epoch,
        slots: [{ slot: { kind: 'table' }, seq: this.seq, live: this.live }],
      },
    }
  }

  confirm(body: ConfirmBody): Record<string, unknown> {
    this.disclosures += 1
    this.live = {
      disclosure_id: `${DISCLOSURE_PREFIX}${String(this.disclosures).padStart(5, '0')}`,
      document_id: body.document_id,
      type: 'npc',
      version: body.version,
      mask: body.mask,
      stale_text: false,
      pending_delivery: false,
    }
    this.epoch += 1
    this.seq += 1
    return this.picture()
  }

  /** A Stop clears the slot and advances the epoch on every narrowing. */
  stop(): Record<string, unknown> {
    this.live = null
    this.epoch += 1
    this.seq += 1
    return this.picture()
  }
}

export interface RevealRouteOptions {
  /** Every request made to a reveal path, as `METHOD /path?query`, in order. */
  requests?: string[]
  /** Every POST body to a Confirm or a Stop, with its path. */
  bodies?: Array<{ path: string; body: unknown }>
}

function answer(route: Route, status: number, body: unknown): Promise<void> {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

const CAMPAIGN_PREFIX = `/campaigns/${CAMPAIGN_ID}`
const REVEAL_PATHS = new Set([
  `${CAMPAIGN_PREFIX}/table-session`,
  `${CAMPAIGN_PREFIX}/reveals`,
  `${CAMPAIGN_PREFIX}/reveals/stop`,
  `${CAMPAIGN_PREFIX}/participants`,
  `${CAMPAIGN_PREFIX}/documents/${DOCUMENT_ID}/seal`,
])

/** Answer the reveal surface, statefully. Anything else under `/campaigns` goes on to the Workbench stubs. */
export async function installRevealRoutes(page: Page, options: RevealRouteOptions = {}): Promise<RevealWorld> {
  const world = new RevealWorld()
  await page.route(
    (url) => REVEAL_PATHS.has(url.pathname),
    async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const path = url.pathname
      const method = request.method()
      options.requests?.push(`${method} ${path}${url.search}`)

      if (method === 'GET' && path === `${CAMPAIGN_PREFIX}/table-session`) return answer(route, 200, tableSessionAnswer())
      if (method === 'GET' && path === `${CAMPAIGN_PREFIX}/reveals`) return answer(route, 200, world.picture())
      if (method === 'GET' && path === `${CAMPAIGN_PREFIX}/participants`) return answer(route, 200, SEAT_PAGE)
      if (method === 'POST' && path === `${CAMPAIGN_PREFIX}/documents/${DOCUMENT_ID}/seal`) return answer(route, 200, DOCUMENT)
      if (method === 'POST' && path === `${CAMPAIGN_PREFIX}/reveals`) {
        const body = request.postDataJSON() as ConfirmBody
        options.bodies?.push({ path, body })
        return answer(route, 200, world.confirm(body))
      }
      if (method === 'POST' && path === `${CAMPAIGN_PREFIX}/reveals/stop`) {
        options.bodies?.push({ path, body: request.postDataJSON() as unknown })
        return answer(route, 200, world.stop())
      }
      return route.fallback()
    },
  )
  return world
}
