/**
 * GameContentCard(+Section+Entry) — z7fl.4 Checkpoint D port.
 *
 * Covers:
 *   - Header: kicker label + icon, Cinzel title, qualifier, badge slot
 *   - Stat strip: label/value cells rendered when `stats` is present
 *   - Body: children (sections/entries) rendered
 *   - Footer citation: rendered only when `source` is present
 *   - Compact density: kicker row hidden, icon moves inline with title
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { GameContentCard, GameContentSection, GameContentEntry } from './GameContentCard'

describe('GameContentCard', () => {
  it('renders the title and kicker label', () => {
    render(<GameContentCard kind="spell" title="Fireball" />)
    expect(screen.getByText('Fireball')).toBeInTheDocument()
    expect(screen.getByText('Spell')).toBeInTheDocument()
  })

  it('renders the qualifier when provided', () => {
    render(<GameContentCard title="Fireball" qualifier="3rd-level evocation" />)
    expect(screen.getByText('3rd-level evocation')).toBeInTheDocument()
  })

  it('renders a custom kindLabel over the default kind label', () => {
    render(<GameContentCard kind="statblock" kindLabel="Custom Label" title="Goblin" />)
    expect(screen.getByText('Custom Label')).toBeInTheDocument()
    expect(screen.queryByText('Stat Block')).not.toBeInTheDocument()
  })

  it('renders the badge slot when provided', () => {
    render(<GameContentCard title="Fireball" badge={<span>CONCENTRATION</span>} />)
    expect(screen.getByText('CONCENTRATION')).toBeInTheDocument()
  })

  it('renders stat-strip label/value cells', () => {
    render(
      <GameContentCard
        title="Fireball"
        stats={[
          { label: 'Casting Time', value: '1 action' },
          { label: 'Range', value: '150 feet' },
        ]}
      />,
    )
    expect(screen.getByText('Casting Time')).toBeInTheDocument()
    expect(screen.getByText('1 action')).toBeInTheDocument()
    expect(screen.getByText('Range')).toBeInTheDocument()
    expect(screen.getByText('150 feet')).toBeInTheDocument()
  })

  it('omits the stat strip entirely when stats is absent', () => {
    const { container } = render(<GameContentCard title="Fireball" />)
    expect(container.querySelector('.game-content-card__stats')).not.toBeInTheDocument()
  })

  it('filters out nullish/empty stat values, keeping numeric zero (PR #46 review)', () => {
    // A minimal valid spell/stat-block payload can leave several stat fields
    // absent; rendering them anyway produced labeled cells with blank values.
    const { container } = render(
      <GameContentCard
        title="Fireball"
        stats={[
          { label: 'Casting Time', value: '1 action' },
          { label: 'Range', value: undefined },
          { label: 'Components', value: '' },
          { label: 'Duration', value: null },
          { label: 'Speed', value: 0 },
        ]}
      />,
    )
    expect(screen.getByText('Casting Time')).toBeInTheDocument()
    expect(screen.queryByText('Range')).not.toBeInTheDocument()
    expect(screen.queryByText('Components')).not.toBeInTheDocument()
    expect(screen.queryByText('Duration')).not.toBeInTheDocument()
    expect(screen.getByText('Speed')).toBeInTheDocument()
    expect(container.querySelectorAll('.game-content-card__stat-cell')).toHaveLength(2)
  })

  it('omits the stat strip entirely when every stat value is empty', () => {
    const { container } = render(
      <GameContentCard
        title="Fireball"
        stats={[
          { label: 'Range', value: undefined },
          { label: 'Duration', value: '' },
        ]}
      />,
    )
    expect(container.querySelector('.game-content-card__stats')).not.toBeInTheDocument()
  })

  it('renders children (body content)', () => {
    render(
      <GameContentCard title="Fireball">
        <p>A bright streak flashes from you.</p>
      </GameContentCard>,
    )
    expect(screen.getByText('A bright streak flashes from you.')).toBeInTheDocument()
  })

  it('renders the source citation footer only when source is present', () => {
    const { rerender } = render(<GameContentCard title="Fireball" source="phb-2024" />)
    expect(screen.getByText('phb-2024')).toBeInTheDocument()

    rerender(<GameContentCard title="Fireball" />)
    expect(screen.queryByText('phb-2024')).not.toBeInTheDocument()
  })

  it('hides decorative icon ligatures from assistive technology (PR #46 review)', () => {
    // Material Symbols spans expose their ligature string ("auto_awesome") as
    // accessible text unless explicitly hidden; the adjacent labels already
    // convey the meaning.
    const { container } = render(<GameContentCard kind="spell" title="Fireball" source="phb-2024" />)
    const kickerIcon = container.querySelector('.game-content-card__kicker-icon')
    const footerIcon = container.querySelector('.game-content-card__footer-icon')
    expect(kickerIcon).toHaveAttribute('aria-hidden', 'true')
    expect(footerIcon).toHaveAttribute('aria-hidden', 'true')
  })

  it('hides the compact kicker icon from assistive technology', () => {
    const { container } = render(<GameContentCard kind="spell" title="Fireball" density="compact" />)
    const compactIcon = container.querySelector('.game-content-card__kicker-icon--compact')
    expect(compactIcon).toHaveAttribute('aria-hidden', 'true')
  })

  it('hides the kicker row in compact density', () => {
    render(<GameContentCard kind="spell" title="Fireball" density="compact" />)
    // "Spell" kicker label is a default-density-only element.
    expect(screen.queryByText('Spell')).not.toBeInTheDocument()
    // The title still renders in compact density.
    expect(screen.getByText('Fireball')).toBeInTheDocument()
  })
})

describe('GameContentSection', () => {
  it('renders a title when provided', () => {
    render(<GameContentSection title="At Higher Levels">text</GameContentSection>)
    expect(screen.getByText('At Higher Levels')).toBeInTheDocument()
  })

  it('renders without a title (no title node)', () => {
    render(<GameContentSection>text</GameContentSection>)
    expect(screen.getByText('text')).toBeInTheDocument()
  })

  it('the first section omits the top rule', () => {
    const { container } = render(<GameContentSection first>text</GameContentSection>)
    const section = container.querySelector('.game-content-section')
    expect(section).toHaveClass('game-content-section--first')
  })
})

describe('GameContentEntry', () => {
  it('renders an italic entry name followed by the text', () => {
    render(<GameContentEntry name="Multiattack">The creature makes two attacks.</GameContentEntry>)
    expect(screen.getByText(/Multiattack/)).toBeInTheDocument()
    expect(screen.getByText(/The creature makes two attacks\./)).toBeInTheDocument()
  })

  it('renders without a name', () => {
    render(<GameContentEntry>Plain entry text.</GameContentEntry>)
    expect(screen.getByText('Plain entry text.')).toBeInTheDocument()
  })
})
