/**
 * e2eStubs.test.ts -- the e2e spec cannot pass on a shape the app would reject
 * (agent-forge-harness-1kg.6.3, T-16, Critic C-18).
 *
 * `ui/e2e/workbenchStubs.ts` answers the Workbench's reads with literal JSON. Every one
 * of those bodies is parsed here with the real contract schemas, and every id with the
 * server's id shape, so a stub that drifts from the contract fails this suite rather than
 * quietly teaching the e2e a shape production never sends.
 */

import { describe, expect, it } from 'vitest'
import {
  CAMPAIGN,
  CAMPAIGN_ID,
  DOCUMENT,
  DOCUMENT_ID,
  HISTORY,
  MISSING_DOCUMENT_ID,
  THREAD_PAGE,
  libraryPage,
} from '../../e2e/workbenchStubs'
import {
  CampaignSchema,
  ConversationPageSchema,
  DocumentHistoryPageSchema,
  LIBRARY_CATEGORIES,
  LibraryPageSchema,
  LibraryQuerySchema,
  parseDocument,
} from './contracts'

/** The server mints a prefix and 22 to 60 base64url characters, and 404s anything else (`campaign_identity.py`). */
const SERVER_ID = /^(cmp|doc)_[A-Za-z0-9_-]{22,60}$/

describe('the e2e stubs are what the contract accepts', () => {
  it('every id is shaped like one the server mints', () => {
    for (const id of [CAMPAIGN_ID, DOCUMENT_ID, MISSING_DOCUMENT_ID]) expect(id).toMatch(SERVER_ID)
    expect(DOCUMENT.document_id).toBe(DOCUMENT_ID)
    expect(DOCUMENT.campaign_id).toBe(CAMPAIGN_ID)
    expect(HISTORY.document_id).toBe(DOCUMENT_ID)
  })

  it('the campaign parses', () => {
    expect(CampaignSchema.safeParse(CAMPAIGN).success).toBe(true)
  })

  it('the thread list parses, and is empty', () => {
    const parsed = ConversationPageSchema.parse(THREAD_PAGE)
    expect(parsed.items).toEqual([])
  })

  it('the document parses as a document this client can open, at version 2', () => {
    const parsed = parseDocument(DOCUMENT)
    expect(parsed.kind).toBe('ok')
    if (parsed.kind === 'ok') {
      expect(parsed.value.data.name).toBe('Ondrey')
      expect(parsed.value.version.number).toBe(2)
    }
  })

  it('a newer type version in a stub would be caught here (the canary for the test above)', () => {
    expect(parseDocument({ ...DOCUMENT, type_version: 99 }).kind).toBe('unknown')
  })

  it('the history parses and holds two versions, newest first', () => {
    const parsed = DocumentHistoryPageSchema.parse(HISTORY)
    expect(parsed.items.map((version) => version.number)).toEqual([2, 1])
  })

  it.each(LIBRARY_CATEGORIES.filter((category) => category !== 'cues'))('the %s library page parses and echoes its campaign and category', (category) => {
    const parsed = LibraryPageSchema.parse(libraryPage(category))
    expect(parsed.campaign_id).toBe(CAMPAIGN_ID)
    expect(parsed.category).toBe(category)
    expect(parsed.items).toHaveLength(category === 'npcs' ? 1 : 0)
  })

  it('the NPC is listed under npcs, with the title the canvas shows', () => {
    const [item] = LibraryPageSchema.parse(libraryPage('npcs')).items
    expect(item.document_id).toBe(DOCUMENT_ID)
    expect(item.title).toBe(DOCUMENT.data.name)
  })

  it('the library query the nav sends is one the contract accepts for each category', () => {
    for (const category of LIBRARY_CATEGORIES.filter((c) => c !== 'cues')) {
      const query = { schema_version: 1, campaign_id: CAMPAIGN_ID, category, search: '', sort: 'recent', archived: false, limit: 5 }
      expect(LibraryQuerySchema.safeParse(query).success).toBe(true)
    }
  })
})
