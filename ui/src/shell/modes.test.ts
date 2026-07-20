/**
 * modes — channel accent metadata (swe1.3).
 *
 * Each of the four channels carries a distinct Aetheril palette accent so the
 * modes are visually differentiable wherever their chips render. The mapping is
 * DS-grounded: Spell=arcane (magic-only role), GM=ember (DM avatar convention),
 * Sage=verdigris (lore/druid green), Rules=gold (gilt/authority).
 */

import { describe, it, expect } from 'vitest'
import { MODES, accentClass } from './modes'
import type { ChatMode } from './AppNav'

describe('mode accents (swe1.3)', () => {
  it('assigns every mode a distinct accent', () => {
    const accents = MODES.map((m) => m.accent)
    expect(new Set(accents).size).toBe(MODES.length)
  })

  it('maps each channel to its agreed palette family', () => {
    const byMode = Object.fromEntries(MODES.map((m) => [m.mode, m.accent])) as Record<ChatMode, string>
    expect(byMode.sage).toBe('verdigris')
    expect(byMode.spell).toBe('arcane')
    expect(byMode.rules).toBe('gold')
    expect(byMode.gm).toBe('ember')
  })

  it('accentClass(mode) returns the mode-accent-- modifier class', () => {
    expect(accentClass('sage')).toBe('mode-accent--verdigris')
    expect(accentClass('spell')).toBe('mode-accent--arcane')
    expect(accentClass('rules')).toBe('mode-accent--gold')
    expect(accentClass('gm')).toBe('mode-accent--ember')
  })
})
