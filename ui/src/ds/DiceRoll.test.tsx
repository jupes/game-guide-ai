/**
 * DiceRoll — behavior #10 tests
 *
 * Covers:
 *   - Formatted math output (mono "dN + modifier = total")
 *   - Crit green (nat 20 on d20) and fumble red (nat 1 on d20) colors
 *   - Reduced-motion: spin animation absent when prefers-reduced-motion is active
 *   - CRITICAL! / FUMBLE labels
 *   - rolling state renders placeholder
 *   - negative modifier uses true minus sign −
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DiceRoll } from './DiceRoll'

// ── helpers ───────────────────────────────────────────────────────────────────

function renderDiceRoll(props: React.ComponentProps<typeof DiceRoll>) {
  return render(<DiceRoll {...props} />)
}

// ── Formatted result output ───────────────────────────────────────────────────

describe('DiceRoll — formatted result output', () => {
  it('renders the die notation and total for a plain roll', () => {
    renderDiceRoll({ die: 8, value: 6, modifier: 3, label: 'Damage' })
    // Total = 6 + 3 = 9; notation "d8 + 3"
    expect(screen.getByText('9')).toBeInTheDocument()
    expect(screen.getByText(/d8/)).toBeInTheDocument()
    expect(screen.getByText(/\+ 3/)).toBeInTheDocument()
  })

  it('renders the label when provided', () => {
    renderDiceRoll({ die: 20, value: 15, label: 'Stealth check' })
    expect(screen.getByText('Stealth check')).toBeInTheDocument()
  })

  it('renders with no modifier when modifier is 0', () => {
    const { container } = renderDiceRoll({ die: 6, value: 4 })
    // Value 4 appears twice (pip face + total) — that's expected structure
    expect(container.querySelector('.dice-roll__total')?.textContent).toBe('4')
    // Should NOT show "+ 0"
    expect(screen.queryByText(/\+ 0/)).not.toBeInTheDocument()
  })

  it('uses a true minus sign − for negative modifiers', () => {
    renderDiceRoll({ die: 20, value: 10, modifier: -2 })
    // Total = 8; notation must use − not -
    const notationEl = screen.getByText(/d20/)
    expect(notationEl.textContent).toContain('−')
    expect(notationEl.textContent).not.toMatch(/d20 - /)
  })

  it('renders the die face value inside the polygon', () => {
    renderDiceRoll({ die: 12, value: 7 })
    // The face value "7" must appear (inside the polygon die chip)
    expect(screen.getAllByText('7').length).toBeGreaterThanOrEqual(1)
  })

  it('renders rolling placeholder when rolling=true', () => {
    renderDiceRoll({ die: 20, rolling: true, label: 'Rolling…' })
    // Face shows '·' and total shows '—'
    expect(screen.getByText('·')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

// ── Crit / fumble coloring ────────────────────────────────────────────────────

describe('DiceRoll — crit and fumble coloring', () => {
  it('applies the crit class when value === die === 20', () => {
    const { container } = renderDiceRoll({ die: 20, value: 20, modifier: 5, label: 'Attack' })
    const critEl = container.querySelector('.dice-roll--nat20')
    expect(critEl).toBeInTheDocument()
  })

  it('shows CRITICAL! badge on nat 20', () => {
    renderDiceRoll({ die: 20, value: 20 })
    expect(screen.getByText('CRITICAL!')).toBeInTheDocument()
  })

  it('applies the fumble class when value === 1 on d20', () => {
    const { container } = renderDiceRoll({ die: 20, value: 1, label: 'Stealth' })
    const fumbleEl = container.querySelector('.dice-roll--nat1')
    expect(fumbleEl).toBeInTheDocument()
  })

  it('shows FUMBLE badge on nat 1', () => {
    renderDiceRoll({ die: 20, value: 1 })
    expect(screen.getByText('FUMBLE')).toBeInTheDocument()
  })

  it('does NOT apply crit class when die !== 20 and value === 20', () => {
    // nat-20 is specifically a d20 max
    const { container } = renderDiceRoll({ die: 12, value: 12 })
    expect(container.querySelector('.dice-roll--nat20')).not.toBeInTheDocument()
  })

  it('does NOT apply crit class when value is not the max', () => {
    const { container } = renderDiceRoll({ die: 20, value: 19 })
    expect(container.querySelector('.dice-roll--nat20')).not.toBeInTheDocument()
    expect(container.querySelector('.dice-roll--nat1')).not.toBeInTheDocument()
  })
})

// ── Reduced-motion: no spin animation ────────────────────────────────────────

describe('DiceRoll — prefers-reduced-motion', () => {
  let originalMatchMedia: typeof window.matchMedia

  beforeEach(() => {
    originalMatchMedia = window.matchMedia
  })

  afterEach(() => {
    window.matchMedia = originalMatchMedia
  })

  it('does NOT apply the rolling animation class when prefers-reduced-motion: reduce', () => {
    // Stub matchMedia to report reduced-motion preference
    window.matchMedia = (query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })

    const { container } = renderDiceRoll({ die: 20, rolling: true })
    const spinEl = container.querySelector('.dice-roll__pip--spinning')
    // The spinning class must not be applied under reduced-motion
    expect(spinEl).not.toBeInTheDocument()
  })

  it('applies the rolling animation class when motion is allowed', () => {
    // Stub matchMedia to report NO reduced-motion preference
    window.matchMedia = (query: string) => ({
      matches: query === '(prefers-reduced-motion: no-preference)',
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })

    const { container } = renderDiceRoll({ die: 20, rolling: true })
    const spinEl = container.querySelector('.dice-roll__pip--spinning')
    expect(spinEl).toBeInTheDocument()
  })
})

// ── Bug fix: border visible (no clip-path on the bordered element) ────────────

describe('DiceRoll — border ring visible (clip-path bug fix)', () => {
  it('the polygon clip is on a background layer, not on the bordered element', () => {
    const { container } = renderDiceRoll({ die: 20, value: 20 })
    // The root element of .dice-roll__pip must NOT have clip-path set inline
    const pip = container.querySelector('.dice-roll__pip')
    expect(pip).toBeInTheDocument()
    // The bordered ring should not have clip-path as an inline style
    const ring = container.querySelector('.dice-roll__ring')
    if (ring) {
      const style = (ring as HTMLElement).style.clipPath
      expect(style).toBe('')
    }
    // The clip is on a dedicated background element
    const bg = container.querySelector('.dice-roll__pip-bg')
    expect(bg).toBeInTheDocument()
  })
})
