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
  ARCHIVED_DOCUMENT_ID,
  ARCHIVED_DOCUMENT_TITLE,
  CAMPAIGN,
  CAMPAIGN_ID,
  CREATED_DOCUMENT_ID,
  CREATED_HISTORY,
  CREATED_TITLE,
  DOCUMENT,
  DOCUMENT_ID,
  HISTORY,
  MISSING_DOCUMENT_ID,
  THREAD_PAGE,
  createdDocument,
  libraryPage,
} from '../../e2e/workbenchStubs'
import {
  CampaignSchema,
  ConversationPageSchema,
  DOC_TYPE_VERSION,
  DocumentCreateRequestSchema,
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
    for (const id of [CAMPAIGN_ID, DOCUMENT_ID, MISSING_DOCUMENT_ID, ARCHIVED_DOCUMENT_ID, CREATED_DOCUMENT_ID]) {
      expect(id).toMatch(SERVER_ID)
    }
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

  // ── The Campaign Library (1kg.6.4, L-8) ──

  it('the archived page lists Velka, parses, and is not in the active list until Restore moves her', () => {
    const archived = LibraryPageSchema.parse(libraryPage('npcs', { archived: true }))
    expect(archived.items.map((item) => item.title)).toEqual([ARCHIVED_DOCUMENT_TITLE])
    expect(archived.items[0].document_id).toBe(ARCHIVED_DOCUMENT_ID)
    expect(archived.items[0].archived).toBe(true)
    expect(LibraryPageSchema.parse(libraryPage('npcs')).items.map((item) => item.title)).toEqual(['Ondrey'])
    const restored = LibraryPageSchema.parse(libraryPage('npcs', { state: { unarchived: true, created: false } }))
    expect(restored.items.map((item) => item.title)).toEqual(['Ondrey', ARCHIVED_DOCUMENT_TITLE])
    expect(LibraryPageSchema.parse(libraryPage('npcs', { archived: true, state: { unarchived: true, created: false } })).items).toEqual([])
  })

  it('a search page filters by title, qualifier and tag, case-insensitively, and parses', () => {
    for (const [search, titles] of [
      ['ond', ['Ondrey']],
      ['ONDREY', ['Ondrey']],
      ['almoner', ['Ondrey']],
      ['harbour', ['Ondrey']],
      ['zz', []],
    ] as const) {
      const page = LibraryPageSchema.parse(libraryPage('npcs', { search }))
      expect(page.items.map((item) => item.title)).toEqual(titles)
    }
    expect(LibraryPageSchema.parse(libraryPage('npcs', { archived: true, search: 'lamp' })).items.map((item) => item.title)).toEqual([
      ARCHIVED_DOCUMENT_TITLE,
    ])
  })

  it('a type page filters Documents by type, and parses', () => {
    const state = { unarchived: false, created: true }
    expect(LibraryPageSchema.parse(libraryPage('documents', { type: 'handout', state })).items).toHaveLength(1)
    expect(LibraryPageSchema.parse(libraryPage('documents', { type: 'lore', state })).items).toEqual([])
    expect(LibraryPageSchema.parse(libraryPage('documents', { state })).items[0].title).toBe(CREATED_TITLE)
  })

  it('the created handout parses as a document this client can open, as the GM at version 1', () => {
    const parsed = parseDocument(createdDocument(CREATED_TITLE))
    expect(parsed.kind).toBe('ok')
    if (parsed.kind === 'ok') {
      expect(parsed.value.document_id).toBe(CREATED_DOCUMENT_ID)
      expect(parsed.value.type).toBe('handout')
      expect(parsed.value.type_version).toBe(DOC_TYPE_VERSION.handout)
      expect(parsed.value.data.name).toBe(CREATED_TITLE)
      expect(parsed.value.version.number).toBe(1)
      expect(parsed.value.version.author).toBe('gm')
    }
    // The canary for the test above: a newer type version would not open.
    expect(parseDocument({ ...createdDocument(CREATED_TITLE), type_version: 99 }).kind).toBe('unknown')
  })

  it('the created document\'s history parses and holds its one version', () => {
    expect(DocumentHistoryPageSchema.parse(CREATED_HISTORY).items.map((version) => version.number)).toEqual([1])
  })

  it('the create body the spec asserts is one the contract accepts, and a stat block without AC and HP is not', () => {
    const body = {
      schema_version: 1,
      command_id: 'cmd_e2eCreateCommand01',
      campaign_id: CAMPAIGN_ID,
      type: 'handout',
      type_version: DOC_TYPE_VERSION.handout,
      data: { name: CREATED_TITLE },
    }
    expect(DocumentCreateRequestSchema.safeParse(body).success).toBe(true)
    expect(DocumentCreateRequestSchema.safeParse({ ...body, type: 'statblock', type_version: 1 }).success).toBe(false)
  })

  it('the library queries the panel sends, with a search, a type, archived and a cursor, are ones the contract accepts', () => {
    const base = { schema_version: 1, campaign_id: CAMPAIGN_ID, sort: 'recent', limit: 25 }
    for (const query of [
      { ...base, category: 'npcs', search: 'zz', archived: false },
      { ...base, category: 'npcs', search: '', archived: true },
      { ...base, category: 'documents', search: '', archived: false, type: 'handout' },
      { ...base, category: 'npcs', search: '', archived: false, cursor: 'c25' },
    ]) {
      expect(LibraryQuerySchema.safeParse(query).success).toBe(true)
    }
  })
})
