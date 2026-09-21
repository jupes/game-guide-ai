import { describe, it, expect } from 'vitest'
import { suggestionFor, toLaneStatus, toLaneSuggestions, toStatBlockCardProps } from './adapters'
import { INVOCATION_STATUSES, ToolResultSchema } from './contracts'
import type { ToolSuggestion } from './contracts'
import { cardResult } from './laneFixtures'

const SUGGESTIONS: ToolSuggestion[] = [
  { tool_id: 'npc', label: 'Write her up', icon: 'person_add', brief: 'the hooded stranger' },
  { tool_id: 'npc', label: 'Write him up', icon: null, brief: 'the ferryman' },
  { tool_id: 'portrait', label: 'Portrait' },
]

describe('toLaneStatus', () => {
  it('maps the wire state "failed" to the lane look "error"', () => {
    expect(toLaneStatus('failed')).toBe('error')
  })

  it('passes every other wire status through', () => {
    const others = INVOCATION_STATUSES.filter((status) => status !== 'failed')
    expect(others.map(toLaneStatus)).toEqual(others)
  })
})

describe('toLaneSuggestions', () => {
  it('produces camelCase chips without wire keys', () => {
    expect(toLaneSuggestions(SUGGESTIONS)).toEqual([
      { id: '0', label: 'Write her up', icon: 'person_add' },
      { id: '1', label: 'Write him up' },
      { id: '2', label: 'Portrait' },
    ])
  })

  it('gives two suggestions for the same tool different ids', () => {
    const ids = toLaneSuggestions(SUGGESTIONS).map((chip) => chip.id)
    expect(new Set(ids).size).toBe(SUGGESTIONS.length)
  })
})

describe('suggestionFor', () => {
  it('finds the wire suggestion a chip was made from, brief included', () => {
    const [, second] = toLaneSuggestions(SUGGESTIONS)
    expect(suggestionFor(SUGGESTIONS, second)).toBe(SUGGESTIONS[1])
  })

  it('is undefined for an id this list never issued', () => {
    for (const id of ['3', '-1', 'npc', '', '1.5']) expect(suggestionFor(SUGGESTIONS, { id })).toBeUndefined()
  })
})

describe('toStatBlockCardProps', () => {
  function statBlock(overrides: Record<string, unknown> = {}) {
    const result = ToolResultSchema.parse(cardResult(overrides))
    if (result.result_kind !== 'card') throw new Error('unreachable')
    return result.card.stat_block
  }

  it('renames the wire keys the widget spells differently', () => {
    const props = toStatBlockCardProps(statBlock())
    expect(props.name).toBe('Drowned Thing')
    expect(props.hitDice).toBe('9d10 + 27')
    expect(props.cr).toBe(5)
    expect(props.actions?.[0].name).toBe('Drag Under')
  })

  it('turns every absent field into `undefined`, never `null`', () => {
    // A `null` reaching the widget printed a literal gap in the qualifier line
    // (PR #46); the card's own code reads `!= null`, so the adapter owes it
    // `undefined` for everything the model did not report.
    const bare = statBlock({
      card: { card_kind: 'stat_block', stat_block: { name: 'Mystery', ac: 10, hp: 4 } },
    })
    const props = toStatBlockCardProps(bare)
    for (const value of Object.values(props)) expect(value).not.toBeNull()
    expect(props.abilities).toBeUndefined()
    expect(props.alignment).toBeUndefined()
    expect(props.legendaryActions).toBeUndefined()
  })

  it('keeps the abilities it was given and drops the ones it was not', () => {
    const partial = statBlock({
      card: {
        card_kind: 'stat_block',
        stat_block: { name: 'Half-known', ac: 12, hp: 20, abilities: { str: 16, cha: null } },
      },
    })
    expect(toStatBlockCardProps(partial).abilities).toEqual({
      str: 16,
      dex: undefined,
      con: undefined,
      int: undefined,
      wis: undefined,
      cha: undefined,
    })
  })
})
