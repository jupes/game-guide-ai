/**
 * a11y.test.tsx — Behavior #22: controls meet the 44px touch floor, expose
 * accessible names, and animations are suppressed under prefers-reduced-motion.
 *
 * CP-F6.1 — Reduced-motion + accessibility pass.
 *
 * Scope: representative interactive controls across the DS and shell.
 * These tests assert the contracts that matter for accessibility; they are
 * not exhaustive pixel-perfect tests.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Button } from './Button'
import { IconButton } from './IconButton'
import { Switch } from './Switch'
import { Chip } from './Chip'
import { TextField } from './TextField'
import { DiceRoll } from './DiceRoll'
import { UserMenu } from '../shell/UserMenu'
import { CurrentUserProvider } from '../shell/currentUser'
import { ThemeProvider } from './theme'

// ── 44px touch floor ──────────────────────────────────────────────────────────

describe('44px touch floor', () => {
  it('Button medium meets the 44px floor via data-touch-target attribute', () => {
    render(<Button>Click me</Button>)
    const btn = screen.getByRole('button', { name: 'Click me' })
    // Button medium/large carry data-touch-target="true"; the CSS rule
    // min-height: var(--aether-touch-min) is driven by data-size="medium".
    expect(btn).toHaveAttribute('data-touch-target', 'true')
    expect(btn).toHaveAttribute('data-size', 'medium')
  })

  it('IconButton medium meets the 44px floor via data-touch-target attribute', () => {
    render(<IconButton icon="send" ariaLabel="Send" size="medium" />)
    const btn = screen.getByRole('button', { name: 'Send' })
    expect(btn).toHaveAttribute('data-touch-target', 'true')
    expect(btn).toHaveAttribute('data-size', 'medium')
  })

  it('Switch button meets the 44px floor via data-touch-target attribute', () => {
    render(<Switch ariaLabel="Dark theme" />)
    const btn = screen.getByRole('switch', { name: 'Dark theme' })
    expect(btn).toHaveAttribute('data-touch-target', 'true')
  })

  it('UserMenu trigger button meets the 44px floor via its CSS class', () => {
    // jsdom does not evaluate stylesheets, so assert the class contract:
    // .user-menu__trigger (UserMenu.css) sets min-height/min-width to 44px.
    render(
      <ThemeProvider>
        <CurrentUserProvider>
          <UserMenu />
        </CurrentUserProvider>
      </ThemeProvider>,
    )
    const btn = screen.getByRole('button', { name: 'Open user menu' })
    expect(btn).toHaveClass('user-menu__trigger')
  })

  it('Chip with onClick exposes role=button and is keyboard-reachable (tabIndex=0)', () => {
    const onClick = vi.fn()
    render(<Chip label="Sage" type="filter" icon="auto_stories" onClick={onClick} />)
    const chip = screen.getByRole('button', { name: 'Sage' })
    expect(chip).toBeInTheDocument()
    expect(chip).toHaveAttribute('tabindex', '0')
  })

  it('Chip without onClick has no button role (static chip)', () => {
    render(<Chip label="Tag" />)
    // A non-interactive chip should not expose button role
    expect(screen.queryByRole('button', { name: 'Tag' })).not.toBeInTheDocument()
  })
})

// ── Accessible names ──────────────────────────────────────────────────────────

describe('accessible names — every icon-only control has an aria-label', () => {
  it('IconButton requires and surfaces ariaLabel', () => {
    render(<IconButton icon="download" ariaLabel="Export chat" />)
    expect(screen.getByRole('button', { name: 'Export chat' })).toBeInTheDocument()
  })

  it('Switch surfaces ariaLabel', () => {
    render(<Switch ariaLabel="Enable dark mode" />)
    expect(screen.getByRole('switch', { name: 'Enable dark mode' })).toBeInTheDocument()
  })

  it('TextField with label has an accessible label', () => {
    render(<TextField label="Prompt" />)
    // The label element is rendered; it's the accessible name for the input.
    expect(screen.getByText('Prompt')).toBeInTheDocument()
  })

  it('Chip with onClick exposes an accessible name from its label', async () => {
    const onClick = vi.fn()
    render(<Chip label="Rules" type="filter" onClick={onClick} />)
    const chip = screen.getByRole('button', { name: 'Rules' })
    expect(chip).toBeInTheDocument()
    // Keyboard activation works (Enter)
    chip.focus()
    await userEvent.keyboard('{Enter}')
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('Chip with onClick is activatable via Space key', async () => {
    const onClick = vi.fn()
    render(<Chip label="Spell" type="filter" onClick={onClick} />)
    const chip = screen.getByRole('button', { name: 'Spell' })
    chip.focus()
    await userEvent.keyboard(' ')
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

// ── prefers-reduced-motion — DiceRoll spin ────────────────────────────────────

describe('prefers-reduced-motion — DiceRoll', () => {
  let originalMatchMedia: typeof window.matchMedia

  beforeEach(() => {
    originalMatchMedia = window.matchMedia
  })

  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: originalMatchMedia,
    })
  })

  it('does NOT add the spinning class when prefers-reduced-motion: reduce is active', () => {
    // Stub matchMedia to report reduced-motion
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: (query: string) => ({
        matches: query === '(prefers-reduced-motion: reduce)',
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    })

    render(<DiceRoll die={20} value={10} rolling />)
    // The pip element should NOT carry the spinning class when reduced-motion is on
    const pip = document.querySelector('.dice-roll__pip')
    expect(pip).not.toHaveClass('dice-roll__pip--spinning')
  })

  it('ADDS the spinning class when prefers-reduced-motion is not requested', () => {
    // Stub matchMedia to report no reduced-motion preference
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: (query: string) => ({
        matches: query !== '(prefers-reduced-motion: reduce)',
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    })

    render(<DiceRoll die={20} value={10} rolling />)
    const pip = document.querySelector('.dice-roll__pip')
    expect(pip).toHaveClass('dice-roll__pip--spinning')
  })

  it('does NOT add the spinning class when rolling=false regardless of motion preference', () => {
    render(<DiceRoll die={20} value={15} rolling={false} />)
    const pip = document.querySelector('.dice-roll__pip')
    expect(pip).not.toHaveClass('dice-roll__pip--spinning')
  })
})

// ── Focus ring — data-attribute-driven assertion ──────────────────────────────

describe('focus ring token coverage', () => {
  it('Button carries no inline style that would override the CSS focus ring', () => {
    render(<Button>Click me</Button>)
    const btn = screen.getByRole('button', { name: 'Click me' })
    // The focus ring is defined in Button.css via :focus-visible — it must not
    // be suppressed by an inline outline:none. Assert the inline style is clean.
    expect(btn.style.outline).toBeFalsy()
  })

  it('IconButton carries no inline style that would override the CSS focus ring', () => {
    render(<IconButton icon="settings" ariaLabel="Settings" />)
    const btn = screen.getByRole('button', { name: 'Settings' })
    expect(btn.style.outline).toBeFalsy()
  })

  it('Switch carries no inline style that would override the CSS focus ring', () => {
    render(<Switch ariaLabel="Toggle" />)
    const btn = screen.getByRole('switch', { name: 'Toggle' })
    expect(btn.style.outline).toBeFalsy()
  })
})
