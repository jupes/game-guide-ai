/**
 * The document fixtures are real wire payloads (agent-forge-harness-1kg.6.2).
 *
 * Every story and every component test below renders one of these, so they are
 * checked here against the contract's own schemas rather than being trusted.
 * A fixture that drifts from the wire would otherwise let the renderers pass
 * their tests against a shape the server never sends.
 */

import { describe, expect, it } from 'vitest'

import { DOCUMENT_TYPE_IDS, parseDocument } from './contracts'
import { documentTypeById } from './registry'
import { DOCUMENT_FIXTURES, documentFixture, hydratedFixture } from './documentFixtures'

describe('every fixture is a document the contract accepts', () => {
  it('covers all eight types, and only those', () => {
    expect(Object.keys(DOCUMENT_FIXTURES).sort()).toEqual([...DOCUMENT_TYPE_IDS].sort())
  })

  it.each(DOCUMENT_TYPE_IDS)('%s parses through parseDocument', (id) => {
    const parsed = parseDocument(DOCUMENT_FIXTURES[id])
    expect(parsed.kind, JSON.stringify(parsed)).toBe('ok')
  })

  it.each(DOCUMENT_TYPE_IDS)('%s fills every field its type declares', (id) => {
    const document = documentFixture(id)
    const type = documentTypeById(id)
    for (const key of Object.keys(type?.fields ?? {})) {
      expect(Object.hasOwn(document.data, key), `${id}.${key} is missing`).toBe(true)
    }
    expect(document.data.name).toBeTruthy()
  })

  it.each(DOCUMENT_TYPE_IDS)('%s keeps no key its type does not declare', (id) => {
    const document = documentFixture(id)
    const type = documentTypeById(id)
    const declared = new Set([...Object.keys(type?.fields ?? {}), 'name', 'qualifier', 'tags'])
    for (const key of Object.keys(document.data)) expect(declared.has(key), key).toBe(true)
  })

  it('keeps structure structured — a list is never one joined string', () => {
    const notes = documentFixture('session-notes')
    expect(Array.isArray(notes.data.present)).toBe(true)
    expect(Array.isArray(notes.data.beats)).toBe(true)
    const statblock = documentFixture('statblock')
    expect(Array.isArray(statblock.data.actions)).toBe(true)
    expect(statblock.data.abilities).toMatchObject({ str: expect.any(Number) })
  })

  it('carries an astral character, so the code-point paths are exercised', () => {
    const npc = documentFixture('npc')
    expect(String(npc.data.notes)).toMatch(/\p{Extended_Pictographic}/u)
  })

  it('carries a bare ampersand, so a double-escape would show as an entity', () => {
    expect(String(documentFixture('lore').data.history)).toContain(' & ')
  })

  it('names an asset by id and never by URL (X-10)', () => {
    const portrait = documentFixture('npc').data.portrait
    expect(portrait).toMatchObject({ asset_id: expect.any(String), media_type: 'image' })
    expect(JSON.stringify(portrait)).not.toMatch(/https?:/)
  })

  it('offers an empty document, so the empty-field presentation has a fixture', () => {
    const empty = documentFixture('npc', { empty: true })
    expect(empty.data.voice).toBe('')
    expect(empty.data.tags).toEqual([])
    expect(empty.data.portrait).toBeNull()
    expect(empty.data.name).toBeTruthy()
  })

  it('offers a hydrated document whose version lists changed fields', () => {
    // CANVAS-28: a hydrated document has a change history and still shows no
    // wash. The fixture makes that testable — the renderer must ignore this.
    expect(hydratedFixture().version.changed_fields.length).toBeGreaterThan(0)
  })
})
