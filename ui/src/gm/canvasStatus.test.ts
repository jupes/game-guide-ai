/**
 * canvasStatus — the pure reads behind the canvas header (1kg.6.1).
 *
 * Covers CANVAS-13's five statuses, REVEAL-13/REVEAL-14/REVEAL-8's reveal
 * readings, X-8's unknown document type, and CANVAS-27's derived version label,
 * author labels, client-side timestamp format and field labels.
 */

import { describe, expect, it } from 'vitest'
import {
  HISTORY_PAGE_SIZE,
  authorLabel,
  canvasRevealRead,
  canvasStatusRead,
  documentTypeRead,
  fieldLabel,
  formatTimestamp,
  versionLabel,
} from './canvasStatus'

describe('CANVAS-13 — the header carries one aggregate save status', () => {
  it('reads Saved, Saving…, Unsaved changes and Conflict — review verbatim', () => {
    expect(canvasStatusRead('saved', false).message).toBe('Saved')
    expect(canvasStatusRead('saving', false).message).toBe('Saving…')
    expect(canvasStatusRead('unsaved', false).message).toBe('Unsaved changes')
    expect(canvasStatusRead('conflict', false).message).toBe('Conflict — review')
  })

  it("reads Couldn't save — Retry only while a retry is wired (STATE-2)", () => {
    expect(canvasStatusRead('error', true).message).toBe("Couldn't save — Retry")
    expect(canvasStatusRead('error', true).retryable).toBe(true)
    expect(canvasStatusRead('error', false).message).toBe("Couldn't save")
  })

  it('names a failure tone for the two failure states and never for the rest', () => {
    expect(canvasStatusRead('error', true).tone).toBe('error')
    expect(canvasStatusRead('conflict', false).tone).toBe('error')
    expect(canvasStatusRead('saved', false).tone).toBe('idle')
    expect(canvasStatusRead('saving', false).tone).toBe('progress')
    expect(canvasStatusRead('unsaved', false).tone).toBe('progress')
  })
})

describe('REVEAL-13 / REVEAL-14 — the header repeats the live projection', () => {
  it('says nothing and offers Reveal to party while the document is hidden', () => {
    const read = canvasRevealRead({ state: 'hidden' })
    expect(read.message).toBeNull()
    expect(read.openLabel).toBe('Reveal to party')
    expect(read.stopLabel).toBeNull()
  })

  it('reads Reveal state unknown — reconnecting, never "nothing revealed"', () => {
    const read = canvasRevealRead({ state: 'unknown' })
    expect(read.message).toBe('Reveal state unknown — reconnecting')
    expect(read.openLabel).toBeNull()
    expect(read.stopLabel).toBe('Stop showing')
  })

  it('names what is live and keeps Stop a separate control', () => {
    const read = canvasRevealRead({ state: 'revealed', summary: 'portrait, name & voice' })
    expect(read.message).toBe('Revealed · portrait, name & voice')
    expect(read.openLabel).toBe('Change what the table sees')
    expect(read.stopLabel).toBe('Stop showing')
    expect(read.note).toBeNull()
  })

  it('REVEAL-8: adds the earlier-version note and offers Update…', () => {
    const read = canvasRevealRead({ state: 'revealed', summary: 'notes', behindLatest: true })
    expect(read.note).toBe('Table is seeing an earlier version')
    expect(read.openLabel).toBe('Update…')
  })
})

describe('X-8 — unknown is not NPC', () => {
  it('reads a known type from the registry', () => {
    expect(documentTypeRead('npc')).toEqual({ label: 'NPC Dossier', icon: 'person', known: true })
  })

  it('reads an unknown type as a neutral placeholder', () => {
    const read = documentTypeRead('bestiary-of-the-deep')
    expect(read.known).toBe(false)
    expect(read.label).toBe('Document')
    expect(read.label).not.toBe('NPC Dossier')
  })
})

describe('CANVAS-27 — version labels, authors and times', () => {
  it('pages 20 entries at a time', () => {
    expect(HISTORY_PAGE_SIZE).toBe(20)
  })

  it('derives the short label the handoff used to send', () => {
    expect(versionLabel(3)).toBe('v3')
  })

  it('names the author You or Assistant', () => {
    expect(authorLabel('gm')).toBe('You')
    expect(authorLabel('assistant')).toBe('Assistant')
  })

  it('formats an ISO timestamp for people, and not as the raw string', () => {
    const formatted = formatTimestamp('2026-09-16T19:36:00Z', 'en-GB')
    expect(formatted).not.toBeNull()
    expect(formatted).not.toBe('2026-09-16T19:36:00Z')
    expect(formatted).toContain('2026')
  })

  it('is locale-aware — two locales order the same instant differently', () => {
    const gb = formatTimestamp('2026-03-04T09:05:00Z', 'en-GB')
    const us = formatTimestamp('2026-03-04T09:05:00Z', 'en-US')
    expect(gb).not.toBe(us)
  })

  it('distinguishes two instants', () => {
    const first = formatTimestamp('2026-03-04T09:05:00Z', 'en-GB')
    const second = formatTimestamp('2026-03-05T09:05:00Z', 'en-GB')
    expect(first).not.toBe(second)
  })

  it('returns null rather than inventing a date for a value it cannot read', () => {
    expect(formatTimestamp('7:36 PM')).toBeNull()
  })

  it('returns null rather than throwing on an ill-formed language tag', () => {
    expect(formatTimestamp('2026-09-16T19:36:00Z', 'not a locale')).toBeNull()
  })
})

describe('changed-field labels', () => {
  it('uses the registry label for a declared field', () => {
    expect(fieldLabel('if_attacked', 'npc')).toBe('If the party attacks')
  })

  it('falls back to the common field labels', () => {
    expect(fieldLabel('qualifier', 'npc')).toBe('Qualifier')
    expect(fieldLabel('name')).toBe('Name')
  })

  it('humanises a key the type does not declare, rather than showing it raw', () => {
    // 1kg.5.3 declared all eight types, so the fallback is no longer "a type
    // whose labels have not landed" but a key from a newer server, which the
    // client strips from the data yet may still meet in `changed_fields`.
    expect(fieldLabel('morale_rating', 'encounter')).toBe('Morale rating')
    expect(fieldLabel('beats', 'quest-log')).toBe('Beats')
    // The same key on the type that DOES declare it comes from the registry.
    expect(fieldLabel('xp_budget', 'encounter')).toBe('XP budget')
  })

  it('leaves a key it cannot humanise alone', () => {
    expect(fieldLabel('_', 'npc')).toBe('_')
  })

  it('does not borrow the NPC labels for an unknown type (X-8)', () => {
    expect(fieldLabel('if_attacked', 'not-a-type')).toBe('If attacked')
    // `constructor` matches the wire's field-key pattern. Read off a plain object
    // without an own-property check it is `Object` itself, not a label.
    expect(fieldLabel('constructor', 'npc')).toBe('Constructor')
    expect(fieldLabel('constructor')).toBe('Constructor')
    expect(fieldLabel('if_attacked')).toBe('If attacked')
  })
})
