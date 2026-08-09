/**
 * StatBlockCard — z7fl.4 Checkpoint D port.
 *
 * Covers (full density is what game-guide-ai actually renders — see
 * ChatPane's replace-not-augment design):
 *   - Name, size/type/alignment qualifier, CR badge
 *   - AC/HP/Speed stat strip, six-ability row with derived modifiers
 *   - Saving throws / skills / immunities / senses / languages "details" grid
 *     (behavior 11/13 — the content compact density would drop)
 *   - At least one trait/action entry rendered (behavior 11/13)
 *   - Compact density hides details grid and traits/actions entirely
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatBlockCard } from './StatBlockCard'

const FULL_PROPS = {
  name: 'Goblin Scout',
  size: 'Small',
  type: 'humanoid',
  alignment: 'neutral evil',
  ac: 15,
  hp: 7,
  hitDice: '2d6',
  speed: '30 ft.',
  abilities: { str: 8, dex: 14, con: 10, int: 10, wis: 8, cha: 8 },
  savingThrows: 'Dex +4',
  skills: 'Stealth +6',
  damageImmunities: 'poison',
  conditionImmunities: 'poisoned',
  senses: 'darkvision 60 ft.',
  languages: 'Common, Goblin',
  cr: '1/4',
  xp: 50,
  traits: [{ name: 'Nimble Escape', text: 'The goblin can take the Disengage or Hide action as a bonus action.' }],
  actions: [{ name: 'Scimitar', text: 'Melee Weapon Attack: +4 to hit.' }],
}

describe('StatBlockCard — full density', () => {
  it('renders the name and size/type/alignment qualifier', () => {
    render(<StatBlockCard {...FULL_PROPS} />)
    expect(screen.getByText('Goblin Scout')).toBeInTheDocument()
    expect(screen.getByText('Small humanoid, neutral evil')).toBeInTheDocument()
  })

  it('renders a CR/XP badge', () => {
    render(<StatBlockCard {...FULL_PROPS} />)
    expect(screen.getByText(/CR 1\/4/)).toBeInTheDocument()
    expect(screen.getByText(/50 XP/)).toBeInTheDocument()
  })

  it('renders the AC/HP/Speed stat strip', () => {
    render(<StatBlockCard {...FULL_PROPS} />)
    expect(screen.getByText('30 ft.')).toBeInTheDocument()
    expect(screen.getByText(/15/)).toBeInTheDocument()
    expect(screen.getByText(/7/)).toBeInTheDocument()
    expect(screen.getByText(/2d6/)).toBeInTheDocument()
  })

  it('renders all six ability scores with derived modifiers', () => {
    render(<StatBlockCard {...FULL_PROPS} />)
    expect(screen.getByText('STR')).toBeInTheDocument()
    expect(screen.getByText('DEX')).toBeInTheDocument()
    expect(screen.getByText('CON')).toBeInTheDocument()
    expect(screen.getByText('INT')).toBeInTheDocument()
    expect(screen.getByText('WIS')).toBeInTheDocument()
    expect(screen.getByText('CHA')).toBeInTheDocument()
    // str/wis/cha 8 -> modifier -1 (three cells); dex 14 -> modifier +2 (derived, never passed in)
    expect(screen.getAllByText('−1')).toHaveLength(3)
    expect(screen.getByText('+2')).toBeInTheDocument()
  })

  it('renders the details grid: saving throws, skills, immunities, senses, languages (dropped by compact)', () => {
    render(<StatBlockCard {...FULL_PROPS} />)
    expect(screen.getByText('Dex +4')).toBeInTheDocument()
    expect(screen.getByText('Stealth +6')).toBeInTheDocument()
    expect(screen.getByText(/poison.*poisoned/)).toBeInTheDocument()
    expect(screen.getByText('darkvision 60 ft.')).toBeInTheDocument()
    expect(screen.getByText('Common, Goblin')).toBeInTheDocument()
  })

  it('renders trait and action entries (dropped by compact)', () => {
    render(<StatBlockCard {...FULL_PROPS} />)
    expect(screen.getByText(/Nimble Escape/)).toBeInTheDocument()
    expect(screen.getByText(/can take the Disengage or Hide/)).toBeInTheDocument()
    expect(screen.getByText(/Scimitar/)).toBeInTheDocument()
    expect(screen.getByText(/Melee Weapon Attack/)).toBeInTheDocument()
  })
})

describe('StatBlockCard — compact density', () => {
  it('still renders AC/HP/Speed inline', () => {
    render(<StatBlockCard {...FULL_PROPS} density="compact" />)
    expect(screen.getByText(/AC 15/)).toBeInTheDocument()
    expect(screen.getByText(/HP 7/)).toBeInTheDocument()
    expect(screen.getByText('30 ft.')).toBeInTheDocument()
  })

  it('hides the details grid (saving throws / skills / immunities)', () => {
    render(<StatBlockCard {...FULL_PROPS} density="compact" />)
    expect(screen.queryByText('Dex +4')).not.toBeInTheDocument()
    expect(screen.queryByText('Stealth +6')).not.toBeInTheDocument()
  })

  it('hides traits and actions entirely', () => {
    render(<StatBlockCard {...FULL_PROPS} density="compact" />)
    expect(screen.queryByText(/Nimble Escape/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Scimitar/)).not.toBeInTheDocument()
  })
})

describe('StatBlockCard — optional fields', () => {
  it('renders with only the core fields (name, ac, hp)', () => {
    render(<StatBlockCard name="Mystery Creature" ac={10} hp={4} />)
    expect(screen.getByText('Mystery Creature')).toBeInTheDocument()
  })
})
