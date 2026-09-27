/**
 * The pure half of the document renderers (agent-forge-harness-1kg.6.2).
 *
 * Everything here is arithmetic, ordering or geometry: no DOM, no React. The
 * components are tested through their roles and names; these are the pieces
 * that would otherwise only be testable through a rendered tree.
 */

import { describe, expect, it } from 'vitest'

import {
  ABILITY_KEYS,
  ABILITY_SCORE_MAX,
  ABILITY_SCORE_MIN,
  DOCUMENT_TYPE_IDS,
  INTEGER_FIELD_MAX,
  INTEGER_FIELD_MIN,
  TEXT_FIELD_MAX_CHARS,
} from './contracts'
import { REGISTRY, documentTypeById, labelFor } from './registry'
import {
  ABILITY_LABELS,
  FIELD_STATES,
  abilityModifier,
  assetPath,
  clearedValue,
  documentFieldReads,
  formatModifier,
  isBlankValue,
  liveRegionMessage,
  readBoundedText,
  readIntegerInput,
  selectionBarPosition,
  selectionOffsets,
  statusOf,
  stepCodePoint,
} from './documentFields'

describe('documentFieldReads — the registry decides order, label and editability', () => {
  it.each(DOCUMENT_TYPE_IDS)('%s lists the common fields first, then its own, in registry order', (id) => {
    const type = documentTypeById(id)
    expect(type).toBeDefined()
    const expected = [...Object.keys(REGISTRY.common_field_rules), ...Object.keys(type?.fields ?? {})]
    expect(documentFieldReads(id).map((field) => field.key)).toEqual(expected)
  })

  it.each(DOCUMENT_TYPE_IDS)('%s takes every label from the registry, never from the key', (id) => {
    const type = documentTypeById(id)
    for (const field of documentFieldReads(id)) {
      expect(field.label).toBe(labelFor(type!, field.key))
      expect(field.label).not.toMatch(/_/)
      expect(field.label).not.toMatch(/&[a-z]+;/)
    }
  })

  it.each(DOCUMENT_TYPE_IDS)('%s carries the declared kind for every field', (id) => {
    const type = documentTypeById(id)
    for (const field of documentFieldReads(id)) {
      const declared = Object.hasOwn(type?.fields ?? {}, field.key)
        ? type?.fields[field.key]
        : REGISTRY.common_field_rules[field.key] && { name: 'text', qualifier: 'text', tags: 'text_list' }[field.key]
      expect(field.kind).toBe(declared)
    }
  })

  it('answers with nothing for a type this bundle does not know (X-8)', () => {
    expect(documentFieldReads('spaceship')).toEqual([])
  })

  it('never resolves a prototype key as a field', () => {
    // `constructor` matches the wire's field-key grammar, so a plain-object
    // lookup would find `Object` itself and label a field `Object`.
    expect(documentFieldReads('npc').some((field) => field.key === 'constructor')).toBe(false)
  })
})

describe('statusOf — a field with no state given is clean', () => {
  it('defaults to clean', () => {
    expect(statusOf({}, 'voice')).toEqual({ state: 'clean' })
  })

  it('reads an own key only', () => {
    expect(statusOf({}, 'constructor')).toEqual({ state: 'clean' })
    expect(statusOf({}, '__proto__')).toEqual({ state: 'clean' })
  })

  it('returns the state it was given', () => {
    expect(statusOf({ voice: { state: 'saving' } }, 'voice')).toEqual({ state: 'saving' })
  })

  it('names the six states of CANVAS-12 and nothing else', () => {
    expect([...FIELD_STATES]).toEqual(['clean', 'editing', 'saving', 'saved', 'error', 'conflict'])
  })
})

describe('liveRegionMessage — one polite region, silent at rest', () => {
  const fields = documentFieldReads('npc')

  it('is empty when nothing has happened', () => {
    expect(liveRegionMessage(fields, {})).toBe('')
  })

  it('stays empty while a field is being typed into, and while it saves', () => {
    expect(liveRegionMessage(fields, { voice: { state: 'editing' } })).toBe('')
    expect(liveRegionMessage(fields, { voice: { state: 'saving' } })).toBe('')
  })

  it('names the field that saved', () => {
    expect(liveRegionMessage(fields, { voice: { state: 'saved' } })).toBe('Voice saved')
  })

  it('names the field that failed, above one that saved', () => {
    expect(liveRegionMessage(fields, { voice: { state: 'saved' }, wants: { state: 'error' } })).toBe(
      "Couldn't save Wants",
    )
  })

  it('puts a conflict above everything else', () => {
    expect(
      liveRegionMessage(fields, { voice: { state: 'error' }, wants: { state: 'conflict' } }),
    ).toBe('Wants changed elsewhere')
  })

  it('explains a CANVAS-24 takeover, because the Edit control is removed rather than disabled', () => {
    expect(liveRegionMessage(fields, {}, ['wants'])).toBe(
      'Assistant is editing Wants. You cannot edit it by hand until it finishes.',
    )
  })

  it('names the takeover with the registry label, never the key', () => {
    expect(liveRegionMessage(fields, {}, ['wants'])).not.toContain('wants')
  })

  it('keeps the takeover below every event, so it never masks a failure', () => {
    expect(liveRegionMessage(fields, { voice: { state: 'error' } }, ['wants'])).toBe("Couldn't save Voice")
    expect(liveRegionMessage(fields, { voice: { state: 'saved' } }, ['wants'])).toBe('Voice saved')
  })

  it('says nothing for a key the type does not declare', () => {
    expect(liveRegionMessage(fields, {}, ['constructor', 'nonesuch'])).toBe('')
  })
})

describe('abilityModifier — 5e arithmetic, and an absent score is not zero', () => {
  it.each([
    [1, -5],
    [8, -1],
    [9, -1],
    [10, 0],
    [11, 0],
    [12, 1],
    [20, 5],
    [0, -5],
    [99, 44],
  ])('a score of %i is %i', (score, expected) => {
    expect(abilityModifier(score)).toBe(expected)
  })

  it('formats with a sign, using the typographic minus', () => {
    expect(formatModifier(2)).toBe('+2')
    expect(formatModifier(0)).toBe('+0')
    expect(formatModifier(-1)).toBe('−1')
  })

  it('labels all six scores in the contract order', () => {
    expect(ABILITY_KEYS.map((key) => ABILITY_LABELS[key])).toEqual([
      'Strength',
      'Dexterity',
      'Constitution',
      'Intelligence',
      'Wisdom',
      'Charisma',
    ])
  })
})

describe('clearedValue and isBlankValue — a kind clears to its own empty', () => {
  it('clears each kind the way the wire contract says', () => {
    expect(clearedValue('text')).toBe('')
    expect(clearedValue('prose')).toBe('')
    expect(clearedValue('text_list')).toEqual([])
    expect(clearedValue('entry_list')).toEqual([])
    expect(clearedValue('asset')).toBeNull()
    expect(clearedValue('integer')).toBeNull()
    expect(clearedValue('abilities')).toBeNull()
  })

  it('clears a kind it does not know to null rather than guessing', () => {
    expect(clearedValue('constructor')).toBeNull()
  })

  it('reads a cleared value as blank', () => {
    expect(isBlankValue('text', '')).toBe(true)
    expect(isBlankValue('prose', '   ')).toBe(true)
    expect(isBlankValue('text_list', [])).toBe(true)
    expect(isBlankValue('entry_list', [])).toBe(true)
    expect(isBlankValue('asset', null)).toBe(true)
    expect(isBlankValue('integer', null)).toBe(true)
    expect(isBlankValue('abilities', null)).toBe(true)
    // One spelling of "no score" (requirement 7e): a block with no key in it.
    expect(isBlankValue('abilities', {})).toBe(true)
  })

  it('never reads a real value as blank — zero included', () => {
    expect(isBlankValue('integer', 0)).toBe(false)
    expect(isBlankValue('text', 'a')).toBe(false)
    expect(isBlankValue('text_list', ['a'])).toBe(false)
    expect(isBlankValue('entry_list', [{ name: 'Bite', text: '' }])).toBe(false)
    expect(isBlankValue('abilities', { str: 0 })).toBe(false)
  })
})

describe('readIntegerInput — the contract bounds, not a free-form number', () => {
  const bounds = { min: INTEGER_FIELD_MIN, max: INTEGER_FIELD_MAX }

  it('reads an empty box as cleared', () => {
    expect(readIntegerInput('', bounds)).toEqual({ ok: true, value: null })
    expect(readIntegerInput('  ', bounds)).toEqual({ ok: true, value: null })
  })

  it('reads a whole number, signed', () => {
    expect(readIntegerInput('17', bounds)).toEqual({ ok: true, value: 17 })
    expect(readIntegerInput('-3', bounds)).toEqual({ ok: true, value: -3 })
  })

  it('refuses anything that is not a whole number', () => {
    expect(readIntegerInput('1.5', bounds)).toEqual({ ok: false, message: 'Whole numbers only' })
    expect(readIntegerInput('ten', bounds)).toEqual({ ok: false, message: 'Whole numbers only' })
    expect(readIntegerInput('1e3', bounds)).toEqual({ ok: false, message: 'Whole numbers only' })
  })

  it('refuses a number outside the bound, and says the bound', () => {
    expect(readIntegerInput(String(INTEGER_FIELD_MAX + 1), bounds)).toEqual({
      ok: false,
      message: `Between ${INTEGER_FIELD_MIN} and ${INTEGER_FIELD_MAX}`,
    })
  })

  it('uses the ability bounds when it is given them', () => {
    const ability = { min: ABILITY_SCORE_MIN, max: ABILITY_SCORE_MAX }
    expect(readIntegerInput('99', ability)).toEqual({ ok: true, value: 99 })
    expect(readIntegerInput('100', ability)).toEqual({ ok: false, message: 'Between 0 and 99' })
    expect(readIntegerInput('-1', ability)).toEqual({ ok: false, message: 'Between 0 and 99' })
  })
})

describe('readBoundedText — the server counts code points, maxLength counts units', () => {
  it('accepts text inside the bound', () => {
    expect(readBoundedText('Quiet, clipped', TEXT_FIELD_MAX_CHARS)).toEqual({ ok: true, value: 'Quiet, clipped' })
  })

  it('counts an astral character once, as the server does', () => {
    // 100 dice are 200 UTF-16 units, so a maxLength of 200 would let them
    // through while the server's 200-code-point bound refuses them.
    const dice = '\u{1F3B2}'.repeat(100)
    expect(readBoundedText(dice, 200)).toEqual({ ok: true, value: dice })
    expect(readBoundedText(dice + 'x'.repeat(101), 200)).toEqual({ ok: false, message: 'At most 200 characters' })
  })
})

describe('selectionOffsets — a span in code points, as EditScope wants it', () => {
  it('measures a plain span', () => {
    expect(selectionOffsets('she wants the signet', 4, 9)).toEqual({ start: 4, end: 9 })
  })

  it('counts an astral character as one', () => {
    const text = '\u{1F3B2}\u{1F3B2}abc'
    // UTF-16 units 4..5 is the single character `a`, which is code points 2..3.
    expect(selectionOffsets(text, 4, 5)).toEqual({ start: 2, end: 3 })
  })

  it('reports a span whose length matches its text, which the contract checks', () => {
    const text = 'a\u{1F3B2}b'
    const { start, end } = selectionOffsets(text, 1, 3)
    expect(end - start).toBe(1)
  })
})

describe('stepCodePoint — a keyboard selection never splits a surrogate pair', () => {
  const text = 'a\u{1F3B2}b'

  it('steps forward over a whole character', () => {
    expect(stepCodePoint(text, 0, 1)).toBe(1)
    expect(stepCodePoint(text, 1, 1)).toBe(3)
    expect(stepCodePoint(text, 3, 1)).toBe(4)
  })

  it('steps back over a whole character', () => {
    expect(stepCodePoint(text, 4, -1)).toBe(3)
    expect(stepCodePoint(text, 3, -1)).toBe(1)
    expect(stepCodePoint(text, 1, -1)).toBe(0)
  })

  it('stops at both ends', () => {
    expect(stepCodePoint(text, 4, 1)).toBe(4)
    expect(stepCodePoint(text, 0, -1)).toBe(0)
  })
})

describe('selectionBarPosition — clear of the selection, inside the pane', () => {
  const container = { left: 0, top: 0, width: 400, height: 600 }
  const bar = { width: 260, height: 40 }

  it('sits above the selection when there is room, and does not cover it', () => {
    const placed = selectionBarPosition({ left: 40, top: 200, width: 120, height: 20 }, container, bar)
    expect(placed.placement).toBe('above')
    expect(placed.top + bar.height).toBeLessThanOrEqual(200)
  })

  it('drops below when the selection is at the top of the pane', () => {
    const placed = selectionBarPosition({ left: 40, top: 4, width: 120, height: 20 }, container, bar)
    expect(placed.placement).toBe('below')
    expect(placed.top).toBeGreaterThanOrEqual(24)
  })

  it('stays inside a 400 px pane when the selection hugs the right edge', () => {
    const placed = selectionBarPosition({ left: 360, top: 200, width: 38, height: 20 }, container, bar)
    expect(placed.left).toBeGreaterThanOrEqual(0)
    expect(placed.left + bar.width).toBeLessThanOrEqual(container.width)
  })

  it('stays inside when the selection hugs the left edge', () => {
    const placed = selectionBarPosition({ left: 0, top: 200, width: 20, height: 20 }, container, bar)
    expect(placed.left).toBe(0)
  })

  it('is measured against the container, not the viewport', () => {
    const scrolled = { left: 120, top: 80, width: 400, height: 600 }
    const placed = selectionBarPosition({ left: 160, top: 280, width: 120, height: 20 }, scrolled, bar)
    expect(placed.top).toBe(200 - bar.height - 8)
  })

  it('never goes negative when the pane is narrower than the bar', () => {
    const placed = selectionBarPosition({ left: 10, top: 200, width: 20, height: 20 }, { left: 0, top: 0, width: 200, height: 600 }, bar)
    expect(placed.left).toBe(0)
  })
})

describe('assetPath — the one route a GM surface may load an image from', () => {
  it('builds the campaign asset path of MS-7', () => {
    expect(assetPath('camp_1', 'asset_9')).toBe('/campaigns/camp_1/assets/asset_9')
  })

  it('escapes both ids, so nothing can climb out of the path', () => {
    expect(assetPath('a/b', 'c?d')).toBe('/campaigns/a%2Fb/assets/c%3Fd')
  })
})
