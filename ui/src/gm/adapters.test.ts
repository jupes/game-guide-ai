import { describe, it, expect } from 'vitest'
import { suggestionFor, toLaneStatus, toLaneSuggestions } from './adapters'
import { INVOCATION_STATUSES } from './contracts'
import type { ToolSuggestion } from './contracts'

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
