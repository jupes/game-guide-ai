import { describe, it, expect } from 'vitest'
import { parseDiceNotation } from './diceNotation'

describe('parseDiceNotation', () => {
  it('parses a positive modifier and preserves the reported total', () => {
    // "1d20+5=18": rolled a 13, +5 = 18.
    const d = parseDiceNotation('You rolled 1d20+5=18 on the check.')
    expect(d).not.toBeNull()
    expect(d!.die).toBe(20)
    expect(d!.modifier).toBe(5)
    expect(d!.total).toBe(18)
    // DiceRoll re-derives total as value + modifier — it must equal 18.
    expect(d!.value + d!.modifier).toBe(18)
    expect(d!.value).toBe(13)
  })

  it('honors a subtracted (negative) modifier instead of flipping it positive', () => {
    // "2d6 - 3 = 9": dice summed to 12, −3 = 9.
    const d = parseDiceNotation('Stealth: 2d6 - 3 = 9')
    expect(d).not.toBeNull()
    expect(d!.die).toBe(6)
    expect(d!.modifier).toBe(-3)
    expect(d!.total).toBe(9)
    expect(d!.value + d!.modifier).toBe(9)
  })

  it('honors a true U+2212 minus sign', () => {
    const d = parseDiceNotation('1d20 − 2 = 7')
    expect(d).not.toBeNull()
    expect(d!.modifier).toBe(-2)
    expect(d!.value + d!.modifier).toBe(7)
  })

  it('parses no-modifier notation', () => {
    const d = parseDiceNotation('1d20 = 14')
    expect(d).not.toBeNull()
    expect(d!.die).toBe(20)
    expect(d!.modifier).toBe(0)
    expect(d!.value).toBe(14)
    expect(d!.total).toBe(14)
  })

  it('exposes a natural-20 face when total minus modifier is 20', () => {
    // "1d20+3=23" → face 20 → DiceRoll can show a crit.
    const d = parseDiceNotation('1d20+3=23')
    expect(d!.value).toBe(20)
    expect(d!.die).toBe(20)
  })

  it('returns null when there is no dice expression', () => {
    expect(parseDiceNotation('The basilisk has AC 15 and 52 hit points.')).toBeNull()
    expect(parseDiceNotation('')).toBeNull()
  })
})
