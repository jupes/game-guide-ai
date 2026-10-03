/**
 * workbenchStubs.ts -- the Workbench's routes for the e2e stack
 * (agent-forge-harness-1kg.6.3, brief section 8, Critic C-18).
 *
 * The e2e stack is the real route table with in-memory auth and NO database, so every
 * `/campaigns...` request answers 503 (tavern.spec.ts). This file stubs exactly the
 * Workbench's reads with `page.route`, and leaves everything else (sign-in, the session,
 * the SPA, the CSP, the origin guard) real.
 *
 * The bodies are literal JSON, and `ui/src/gm/e2eStubs.test.ts` parses every one with
 * the real contract schemas, so this spec cannot pass on a shape the app would reject.
 * The ids look like the server's (a prefix and 22 to 60 base64url characters): the
 * server answers 404 to anything else, so a stub id that is not shaped like one would
 * prove nothing about the real route.
 *
 * The Campaign Library (1kg.6.4) adds three writes to the stub: `POST /campaigns/{cid}/documents`
 * (a handout, named from the body), `POST …/{did}/unarchive` (204; it moves Velka back to the
 * active NPCs) and a library that honours `archived`, `search` and `type` the way the server
 * does (a case-insensitive substring over title, qualifier and tags).
 */

import type { Page, Route } from '@playwright/test'

export const CAMPAIGN_ID = 'cmp_e2eWorkbenchCampaign000001'
// The id never contains the title: the spec asserts the URL carries one and not the other.
export const DOCUMENT_ID = 'doc_e2eNpcDocument00000000001'
export const MISSING_DOCUMENT_ID = 'doc_e2eMissingDocument000001'
export const DOCUMENT_TITLE = 'Ondrey'
/** The archived NPC the library's Archived filter lists, and Restore brings back. */
export const ARCHIVED_DOCUMENT_ID = 'doc_e2eArchivedNpcDocument0001'
export const ARCHIVED_DOCUMENT_TITLE = 'Velka'
/** The handout the library's New makes. */
export const CREATED_DOCUMENT_ID = 'doc_e2eCreatedHandout00000001'

export const CAMPAIGN = {
  schema_version: 1,
  campaign_id: CAMPAIGN_ID,
  name: 'The Drowned Coast',
  created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z',
  archived_at: null,
  concluded_at: null,
  tone: null,
  game_system: 'dnd5e',
  avatar_icon: 'sailing',
  avatar_tone: 'ember',
  badge: null,
  seat_count: 0,
  last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null,
  dormant: false,
}

/** The GM channel's thread list for the campaign: none yet. */
export const THREAD_PAGE = { schema_version: 1, items: [], next_cursor: null }

/** An NPC with no portrait (the asset route is not stubbed), at version 2. */
export const DOCUMENT = {
  schema_version: 1,
  document_id: DOCUMENT_ID,
  campaign_id: CAMPAIGN_ID,
  type: 'npc',
  type_version: 1,
  data: {
    name: DOCUMENT_TITLE,
    qualifier: 'Harbour almoner',
    tags: ['harbour'],
    voice: 'Quiet, clipped, never raised',
    wants: 'The family signet.',
  },
  write_revision: 4,
  version: {
    number: 2,
    author: 'gm',
    summary: 'Voice, by hand',
    created_at: '2026-09-16T19:36:00Z',
    sealed: true,
    changed_fields: ['voice'],
    restored_from: null,
  },
  archived: false,
  created_at: '2026-09-14T11:02:00Z',
  updated_at: '2026-09-16T19:36:00Z',
}

/** Newest first: version 2 and the one before it. */
export const HISTORY = {
  schema_version: 1,
  document_id: DOCUMENT_ID,
  items: [
    {
      number: 2,
      author: 'gm',
      summary: 'Voice, by hand',
      created_at: '2026-09-16T19:36:00Z',
      sealed: true,
      changed_fields: ['voice'],
      restored_from: null,
    },
    {
      number: 1,
      author: 'assistant',
      summary: 'First draft',
      created_at: '2026-09-14T11:02:00Z',
      sealed: true,
      changed_fields: [],
      restored_from: null,
    },
  ],
  next_cursor: null,
}

/** What the stub server has been told so far: Restore moves Velka to the active NPCs, and New adds the handout. */
export interface LibraryState {
  unarchived: boolean
  created: boolean
}

export interface LibraryFilter {
  archived?: boolean
  search?: string
  type?: string | null
  state?: LibraryState
}

interface StubItem {
  document_id: string
  type: string
  title: string
  qualifier: string
  tags: string[]
  archived: boolean
  updated_at: string
}

function itemsFor(category: string, state: LibraryState): StubItem[] {
  if (category === 'npcs') {
    return [
      {
        document_id: DOCUMENT_ID,
        type: 'npc',
        title: DOCUMENT_TITLE,
        qualifier: 'Harbour almoner',
        tags: ['harbour'],
        archived: false,
        updated_at: '2026-09-16T19:36:00Z',
      },
      {
        document_id: ARCHIVED_DOCUMENT_ID,
        type: 'npc',
        title: ARCHIVED_DOCUMENT_TITLE,
        qualifier: 'Lamp-keeper',
        tags: ['coast'],
        archived: !state.unarchived,
        updated_at: '2026-09-15T08:00:00Z',
      },
    ]
  }
  if (category === 'documents' && state.created) {
    return [
      {
        document_id: CREATED_DOCUMENT_ID,
        type: 'handout',
        title: CREATED_TITLE,
        qualifier: '',
        tags: [],
        archived: false,
        updated_at: '2026-09-16T20:00:00Z',
      },
    ]
  }
  return []
}

/** The name the library gives a new handout (WORKBENCH_COPY.untitled). */
export const CREATED_TITLE = 'Untitled Player Handout'

/** A library page for `category`, filtered as the server filters it. It echoes the campaign and category (LIB-25). With no filter it is the active items: the NPC in `npcs`, nothing in the others. */
export function libraryPage(category: string, filter: LibraryFilter = {}): Record<string, unknown> {
  const needle = (filter.search ?? '').trim().toLowerCase()
  const items = itemsFor(category, filter.state ?? { unarchived: false, created: false })
    .filter((item) => item.archived === (filter.archived ?? false))
    .filter((item) => filter.type === undefined || filter.type === null || item.type === filter.type)
    .filter(
      (item) =>
        needle === '' || [item.title, item.qualifier, ...item.tags].some((text) => text.toLowerCase().includes(needle)),
    )
  return { schema_version: 1, campaign_id: CAMPAIGN_ID, category, items, next_cursor: null }
}

/** The handout `POST /campaigns/{cid}/documents` answers 201 with: server-shaped, at version 1 by the GM. */
export function createdDocument(name: string): Record<string, unknown> {
  return {
    schema_version: 1,
    document_id: CREATED_DOCUMENT_ID,
    campaign_id: CAMPAIGN_ID,
    type: 'handout',
    type_version: 1,
    data: { name },
    write_revision: 1,
    version: {
      number: 1,
      author: 'gm',
      summary: 'Created',
      created_at: '2026-09-16T20:00:00Z',
      sealed: true,
      changed_fields: [],
      restored_from: null,
    },
    archived: false,
    created_at: '2026-09-16T20:00:00Z',
    updated_at: '2026-09-16T20:00:00Z',
  }
}

/** The one version of a document just made. */
export const CREATED_HISTORY = {
  schema_version: 1,
  document_id: CREATED_DOCUMENT_ID,
  items: [
    {
      number: 1,
      author: 'gm',
      summary: 'Created',
      created_at: '2026-09-16T20:00:00Z',
      sealed: true,
      changed_fields: [],
      restored_from: null,
    },
  ],
  next_cursor: null,
}

export const NOT_FOUND = { code: 'not_found', message: 'not found', retryable: false }

export interface WorkbenchRouteOptions {
  /** Every request the page made to a stubbed prefix, as `METHOD /path?query`, in order. */
  requests?: string[]
  /** The JSON body of every POST, in order, beside its path: what the spec asserts a write carried. */
  bodies?: Array<{ path: string; body: Record<string, unknown> }>
}

function answer(route: Route, status: number, body: unknown): Promise<void> {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

/**
 * Answer the Workbench's reads by method and path. Anything under `/campaigns` or
 * `/conversations` that is not one of them goes on to the real stack (a 503 here).
 */
export async function installWorkbenchRoutes(page: Page, options: WorkbenchRouteOptions = {}): Promise<void> {
  const state: LibraryState = { unarchived: false, created: false }
  await page.route(
    (url) => /^\/(campaigns|conversations)(\/|$)/.test(url.pathname),
    async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const path = url.pathname
      options.requests?.push(`${request.method()} ${path}${url.search}`)
      if (request.method() === 'POST' && request.postData() !== null) {
        options.bodies?.push({ path, body: request.postDataJSON() as Record<string, unknown> })
      }

      if (request.method() === 'GET' && path === `/campaigns/${CAMPAIGN_ID}`) return answer(route, 200, CAMPAIGN)
      if (request.method() === 'GET' && path === '/conversations' && url.searchParams.get('campaign_id') === CAMPAIGN_ID) {
        return answer(route, 200, THREAD_PAGE)
      }
      if (request.method() === 'POST' && path === `/campaigns/${CAMPAIGN_ID}/library`) {
        const { category, archived, search, type } = request.postDataJSON() as {
          category: string
          archived?: boolean
          search?: string
          type?: string | null
        }
        return answer(route, 200, libraryPage(category, { archived, search, type, state }))
      }
      if (request.method() === 'POST' && path === `/campaigns/${CAMPAIGN_ID}/documents`) {
        const { data } = request.postDataJSON() as { data: { name: string } }
        state.created = true
        return answer(route, 201, createdDocument(data.name))
      }
      if (request.method() === 'POST' && path === `/campaigns/${CAMPAIGN_ID}/documents/${ARCHIVED_DOCUMENT_ID}/unarchive`) {
        state.unarchived = true
        return route.fulfill({ status: 204 })
      }
      if (request.method() === 'GET' && path === `/campaigns/${CAMPAIGN_ID}/documents/${CREATED_DOCUMENT_ID}`) {
        return answer(route, 200, createdDocument(CREATED_TITLE))
      }
      if (request.method() === 'GET' && path === `/campaigns/${CAMPAIGN_ID}/documents/${CREATED_DOCUMENT_ID}/versions`) {
        return answer(route, 200, CREATED_HISTORY)
      }
      if (request.method() === 'GET' && path === `/campaigns/${CAMPAIGN_ID}/documents/${DOCUMENT_ID}`) {
        return answer(route, 200, DOCUMENT)
      }
      if (request.method() === 'GET' && path === `/campaigns/${CAMPAIGN_ID}/documents/${DOCUMENT_ID}/versions`) {
        return answer(route, 200, HISTORY)
      }
      if (request.method() === 'GET' && path === `/campaigns/${CAMPAIGN_ID}/documents/${MISSING_DOCUMENT_ID}`) {
        return answer(route, 404, NOT_FOUND)
      }
      return route.fallback()
    },
  )
}
