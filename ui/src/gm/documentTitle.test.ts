/**
 * documentTitle.test.ts -- what a document is called (agent-forge-harness-1kg.6.3).
 */

import { describe, expect, it } from 'vitest'
import { DOCUMENT_TYPE_IDS } from './contracts'
import { documentFixture } from './documentFixtures'
import { documentTitle } from './documentTitle'
import { documentTypeById } from './registry'

describe('documentTitle', () => {
  it('is the document’s name', () => {
    expect(documentTitle(documentFixture('npc'))).toBe('Sister Ondrey Vashe')
  })

  it.each(DOCUMENT_TYPE_IDS)('%s is never read out as nothing', (id) => {
    const named = documentFixture(id)
    const empty = { ...named, data: { ...named.data, name: '   ' } }
    expect(documentTitle(empty)).toBe(`Untitled ${documentTypeById(id)?.label}`)
    const missing = { ...named, data: Object.fromEntries(Object.entries(named.data).filter(([key]) => key !== 'name')) }
    expect(documentTitle(missing)).toBe(`Untitled ${documentTypeById(id)?.label}`)
  })

  it('does not read a name off the prototype', () => {
    const named = documentFixture('npc')
    const data = Object.create({ name: 'from the prototype' }) as typeof named.data
    expect(documentTitle({ ...named, data })).toBe('Untitled NPC Dossier')
  })

  it('ignores a name that is not text', () => {
    const named = documentFixture('npc')
    expect(documentTitle({ ...named, data: { ...named.data, name: 42 } })).toBe('Untitled NPC Dossier')
  })
})
