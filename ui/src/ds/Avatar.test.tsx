/**
 * Avatar — Behavior #8
 *
 * Tests:
 *  - Renders initials from name (first letters of first 2 words, uppercased)
 *  - Single-word name produces 1 initial
 *  - Renders icon variant when icon prop given
 *  - Renders image when src is given (no initials)
 *  - All 4 tones: gold, ember, verdigris, arcane — apply appropriate class
 *  - Default tone is gold
 *  - ring prop adds gilt ring class
 *  - size prop defaults to 40
 *  - DM role (ember tone with icon) is supported
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Avatar } from './Avatar'

describe('Avatar — initials fallback', () => {
  it('derives initials from a two-word name', () => {
    render(<Avatar name="Thalia Brightwood" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').textContent).toBe('TB')
  })

  it('derives initials from a three-word name (max 2 initials)', () => {
    render(<Avatar name="Sir Aldric Vane" data-testid="avatar" />)
    // Only first 2 words
    expect(screen.getByTestId('avatar').textContent).toBe('SA')
  })

  it('derives a single initial from a one-word name', () => {
    render(<Avatar name="Gorath" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').textContent).toBe('G')
  })

  it('uppercases the initials', () => {
    render(<Avatar name="thalia brightwood" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').textContent).toBe('TB')
  })

  it('renders empty when name is empty string', () => {
    render(<Avatar name="" data-testid="avatar" />)
    // Should render without crashing, content will be empty or just whitespace
    expect(screen.getByTestId('avatar')).toBeInTheDocument()
  })
})

describe('Avatar — icon variant', () => {
  it('renders a material-symbols-rounded icon when icon prop is provided', () => {
    render(<Avatar icon="auto_stories" data-testid="avatar" />)
    const icon = screen.getByTestId('avatar').querySelector('.material-symbols-rounded')
    expect(icon).not.toBeNull()
    expect(icon?.textContent).toBe('auto_stories')
  })

  it('prefers src over icon when both provided', () => {
    render(<Avatar src="https://example.com/avatar.png" icon="person" data-testid="avatar" />)
    // When src is set, should render the image (no icon text fallback)
    const avatar = screen.getByTestId('avatar')
    const icon = avatar.querySelector('.material-symbols-rounded')
    expect(icon).toBeNull()
  })

  it('icon takes precedence over initials when src is absent', () => {
    render(<Avatar name="Gorath" icon="shield" data-testid="avatar" />)
    const avatar = screen.getByTestId('avatar')
    const icon = avatar.querySelector('.material-symbols-rounded')
    expect(icon).not.toBeNull()
    // The text content of the icon span should be the icon name, not initials
    expect(icon?.textContent).toBe('shield')
  })
})

describe('Avatar — image variant', () => {
  it('does not render initials when src is provided', () => {
    render(<Avatar src="https://example.com/x.png" name="Thalia Brightwood" data-testid="avatar" />)
    const avatar = screen.getByTestId('avatar')
    // No text content (initials) — the avatar uses a background image
    // The avatar element itself should have no text children that are initials
    const textContent = avatar.textContent?.trim()
    expect(textContent).toBe('')
  })
})

describe('Avatar — tones', () => {
  it('applies gold tone class by default', () => {
    render(<Avatar name="Mira" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').className).toMatch(/avatar--gold/)
  })

  it('applies ember tone class', () => {
    render(<Avatar name="Gorath" tone="ember" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').className).toMatch(/avatar--ember/)
  })

  it('applies verdigris tone class', () => {
    render(<Avatar name="Sylva" tone="verdigris" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').className).toMatch(/avatar--verdigris/)
  })

  it('applies arcane tone class', () => {
    render(<Avatar name="Thalia" tone="arcane" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').className).toMatch(/avatar--arcane/)
  })
})

describe('Avatar — ring', () => {
  it('adds ring class when ring=true', () => {
    render(<Avatar name="DM" ring data-testid="avatar" />)
    expect(screen.getByTestId('avatar').className).toMatch(/avatar--ring/)
  })

  it('does not add ring class by default', () => {
    render(<Avatar name="Player" data-testid="avatar" />)
    expect(screen.getByTestId('avatar').className).not.toMatch(/avatar--ring/)
  })
})

describe('Avatar — size', () => {
  it('defaults to 40px', () => {
    render(<Avatar name="Test" data-testid="avatar" />)
    const el = screen.getByTestId('avatar')
    expect(el.style.width).toBe('40px')
    expect(el.style.height).toBe('40px')
  })

  it('respects custom size', () => {
    render(<Avatar name="Test" size={56} data-testid="avatar" />)
    const el = screen.getByTestId('avatar')
    expect(el.style.width).toBe('56px')
    expect(el.style.height).toBe('56px')
  })
})

describe('Avatar — standard HTML attribute passthrough (02t.6)', () => {
  it('forwards aria-label and onClick to the root element', () => {
    const onClick = vi.fn()
    render(<Avatar name="Thalia" aria-label="Open profile" onClick={onClick} data-testid="avatar" />)
    const el = screen.getByTestId('avatar')
    expect(el.getAttribute('aria-label')).toBe('Open profile')
    fireEvent.click(el)
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

describe('Avatar — DM ember role', () => {
  it('supports ember tone with icon for DM role', () => {
    render(<Avatar icon="auto_stories" tone="ember" ring data-testid="avatar" />)
    const avatar = screen.getByTestId('avatar')
    expect(avatar.className).toMatch(/avatar--ember/)
    expect(avatar.className).toMatch(/avatar--ring/)
    const icon = avatar.querySelector('.material-symbols-rounded')
    expect(icon?.textContent).toBe('auto_stories')
  })
})
