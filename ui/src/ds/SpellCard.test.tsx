/**
 * SpellCard — z7fl.4 Checkpoint D port.
 *
 * Covers (full density is what game-guide-ai actually renders, alongside the
 * prose bubble — see ChatPane's additive design, plans/drafts/structured-
 * content-widgets.md Checkpoint E):
 *   - Name, level/school qualifier (including cantrip), stat strip
 *   - Material-component text and class chips shown in FULL density
 *     (behavior 11/12 — the content compact density would drop)
 *   - Concentration / ritual badges
 *   - Description + "At Higher Levels" section
 *   - Compact density hides material text and class chips
 *   - Missing `level` never renders a false "cantrip" claim (PR #46 review)
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SpellCard } from './SpellCard'

const FULL_PROPS = {
  name: 'Fireball',
  level: 3,
  school: 'evocation',
  castingTime: '1 action',
  range: '150 feet',
  duration: 'Instantaneous',
  components: { v: true, s: true, m: 'a tiny ball of bat guano and sulfur' },
  description: 'A bright streak flashes from you to a point you choose within range.',
  higherLevels: 'the damage increases by 1d6 for each slot level above 3rd',
  classes: ['Sorcerer', 'Wizard'],
  concentration: false,
  ritual: false,
}

describe('SpellCard — full density', () => {
  it('renders the name and level/school qualifier', () => {
    render(<SpellCard {...FULL_PROPS} />)
    expect(screen.getByText('Fireball')).toBeInTheDocument()
    expect(screen.getByText(/3rd-level evocation/)).toBeInTheDocument()
  })

  it('renders a cantrip qualifier for level 0', () => {
    render(<SpellCard name="Fire Bolt" level={0} school="evocation" description="x" />)
    expect(screen.getByText(/evocation cantrip/)).toBeInTheDocument()
  })

  it('never claims "cantrip" when level is simply absent (PR #46 review)', () => {
    // level is optional on the backend; defaulting an unknown level to 0
    // would misrepresent "we don't know" as "this is a cantrip."
    render(<SpellCard name="Mystery Spell" school="evocation" description="x" />)
    expect(screen.queryByText(/cantrip/)).not.toBeInTheDocument()
    // school alone still renders, just without a false level claim.
    expect(screen.getByText('evocation')).toBeInTheDocument()
  })

  it('renders no qualifier at all when both level and school are absent', () => {
    render(<SpellCard name="Mystery Spell" description="x" />)
    expect(screen.queryByText(/cantrip|level/)).not.toBeInTheDocument()
  })

  it('renders the casting time / range / components / duration stat strip', () => {
    render(<SpellCard {...FULL_PROPS} />)
    expect(screen.getByText('1 action')).toBeInTheDocument()
    expect(screen.getByText('150 feet')).toBeInTheDocument()
    expect(screen.getByText('Instantaneous')).toBeInTheDocument()
    expect(screen.getByText('V S M')).toBeInTheDocument()
  })

  it('renders the material-component text (dropped by compact density)', () => {
    render(<SpellCard {...FULL_PROPS} />)
    expect(screen.getByText('a tiny ball of bat guano and sulfur')).toBeInTheDocument()
  })

  it('renders class chips (dropped by compact density)', () => {
    render(<SpellCard {...FULL_PROPS} />)
    expect(screen.getByText('Sorcerer')).toBeInTheDocument()
    expect(screen.getByText('Wizard')).toBeInTheDocument()
  })

  it('renders the description', () => {
    render(<SpellCard {...FULL_PROPS} />)
    expect(
      screen.getByText('A bright streak flashes from you to a point you choose within range.'),
    ).toBeInTheDocument()
  })

  it('renders an "At Higher Levels" section when higherLevels is provided', () => {
    render(<SpellCard {...FULL_PROPS} />)
    expect(screen.getByText('At Higher Levels')).toBeInTheDocument()
    expect(
      screen.getByText('the damage increases by 1d6 for each slot level above 3rd'),
    ).toBeInTheDocument()
  })

  it('omits the "At Higher Levels" section when absent', () => {
    render(<SpellCard {...FULL_PROPS} higherLevels={undefined} />)
    expect(screen.queryByText('At Higher Levels')).not.toBeInTheDocument()
  })

  it('renders a CONCENTRATION badge when concentration is true', () => {
    render(<SpellCard {...FULL_PROPS} concentration />)
    expect(screen.getByText('CONCENTRATION')).toBeInTheDocument()
  })

  it('renders a RITUAL badge when ritual is true (and concentration is false)', () => {
    render(<SpellCard {...FULL_PROPS} ritual concentration={false} />)
    expect(screen.getByText('RITUAL')).toBeInTheDocument()
  })
})

describe('SpellCard — compact density', () => {
  it('hides the material-component text', () => {
    render(<SpellCard {...FULL_PROPS} density="compact" />)
    expect(screen.queryByText('a tiny ball of bat guano and sulfur')).not.toBeInTheDocument()
  })

  it('hides the class chips', () => {
    render(<SpellCard {...FULL_PROPS} density="compact" />)
    expect(screen.queryByText('Sorcerer')).not.toBeInTheDocument()
  })

  it('still renders the description', () => {
    render(<SpellCard {...FULL_PROPS} density="compact" />)
    expect(
      screen.getByText('A bright streak flashes from you to a point you choose within range.'),
    ).toBeInTheDocument()
  })
})
