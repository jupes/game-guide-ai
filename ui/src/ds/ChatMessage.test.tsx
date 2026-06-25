/**
 * ChatMessage — behavior #11 tests
 *
 * Covers:
 *   - role="dm" renders with dm role class, Spectral serif body class, left-aligned
 *   - role="player" renders with player role class, ember bubble, right-aligned
 *   - role="system" renders as centered muted note
 *   - author and time render for dm and player
 *   - DM default author is "Dungeon Master"
 *   - player default author is "You"
 *   - asymmetric bubble radius: tail corner squared toward the author side
 *   - children content renders inside the bubble
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ChatMessage } from './ChatMessage'

// ── helpers ───────────────────────────────────────────────────────────────────

function renderMessage(props: React.ComponentProps<typeof ChatMessage>) {
  return render(<ChatMessage {...props} />)
}

// ── role="dm" ─────────────────────────────────────────────────────────────────

describe('ChatMessage — role="dm"', () => {
  it('renders with the dm role class', () => {
    const { container } = renderMessage({ role: 'dm', children: 'The door creaks open.' })
    expect(container.querySelector('.chat-message--dm')).toBeInTheDocument()
  })

  it('defaults author to "Dungeon Master"', () => {
    renderMessage({ role: 'dm', children: 'Narration.' })
    expect(screen.getByText('Dungeon Master')).toBeInTheDocument()
  })

  it('uses a custom author when provided', () => {
    renderMessage({ role: 'dm', author: 'Elara', children: 'Narration.' })
    expect(screen.getByText('Elara')).toBeInTheDocument()
  })

  it('renders the time label when provided', () => {
    renderMessage({ role: 'dm', time: '8:42 PM', children: 'Narration.' })
    expect(screen.getByText('8:42 PM')).toBeInTheDocument()
  })

  it('applies the Spectral body serif class to the DM bubble', () => {
    const { container } = renderMessage({ role: 'dm', children: 'Narration text.' })
    // DM narration bubble must carry the body-serif class for Spectral
    const bubble = container.querySelector('.chat-message__bubble--body-serif')
    expect(bubble).toBeInTheDocument()
  })

  it('renders children content', () => {
    renderMessage({ role: 'dm', children: 'The tavern falls silent.' })
    expect(screen.getByText('The tavern falls silent.')).toBeInTheDocument()
  })

  it('is left-aligned (row direction, not row-reverse)', () => {
    const { container } = renderMessage({ role: 'dm', children: 'Left side.' })
    const root = container.querySelector('.chat-message--dm')
    // DM messages must NOT have the player/right-align class
    expect(root).not.toHaveClass('chat-message--player')
  })
})

// ── role="player" ─────────────────────────────────────────────────────────────

describe('ChatMessage — role="player"', () => {
  it('renders with the player role class', () => {
    const { container } = renderMessage({ role: 'player', children: 'I look around.' })
    expect(container.querySelector('.chat-message--player')).toBeInTheDocument()
  })

  it('defaults author to "You"', () => {
    renderMessage({ role: 'player', children: 'I act.' })
    expect(screen.getByText('You')).toBeInTheDocument()
  })

  it('uses a custom author when provided', () => {
    renderMessage({ role: 'player', author: 'Thalia', children: 'I scan the room.' })
    expect(screen.getByText('Thalia')).toBeInTheDocument()
  })

  it('renders the time label when provided', () => {
    renderMessage({ role: 'player', time: '8:43 PM', children: 'I act.' })
    expect(screen.getByText('8:43 PM')).toBeInTheDocument()
  })

  it('applies the ember bubble class', () => {
    const { container } = renderMessage({ role: 'player', children: 'Player text.' })
    const bubble = container.querySelector('.chat-message__bubble--ember')
    expect(bubble).toBeInTheDocument()
  })

  it('is right-aligned (row-reverse direction)', () => {
    const { container } = renderMessage({ role: 'player', children: 'Right side.' })
    expect(container.querySelector('.chat-message--player')).toBeInTheDocument()
    expect(container.querySelector('.chat-message--dm')).not.toBeInTheDocument()
  })

  it('does NOT apply the body-serif class (player uses ui font)', () => {
    const { container } = renderMessage({ role: 'player', children: 'Player text.' })
    expect(container.querySelector('.chat-message__bubble--body-serif')).not.toBeInTheDocument()
  })
})

// ── Asymmetric bubble radius ──────────────────────────────────────────────────

describe('ChatMessage — asymmetric bubble radius', () => {
  it('DM bubble has the tail-left class (corner squared toward left/avatar)', () => {
    const { container } = renderMessage({ role: 'dm', children: 'Text.' })
    const bubble = container.querySelector('.chat-message__bubble')
    // DM: tail is top-left squared (avatar is left)
    expect(bubble).toHaveClass('chat-message__bubble--tail-left')
  })

  it('player bubble has the tail-right class (corner squared toward right/avatar)', () => {
    const { container } = renderMessage({ role: 'player', children: 'Text.' })
    const bubble = container.querySelector('.chat-message__bubble')
    // Player: tail is top-right squared (avatar is right)
    expect(bubble).toHaveClass('chat-message__bubble--tail-right')
  })
})

// ── role="system" ─────────────────────────────────────────────────────────────

describe('ChatMessage — role="system"', () => {
  it('renders with the system role class', () => {
    const { container } = renderMessage({ role: 'system', children: 'Quest updated.' })
    expect(container.querySelector('.chat-message--system')).toBeInTheDocument()
  })

  it('renders as a centered notice (no author, no avatar)', () => {
    const { container } = renderMessage({ role: 'system', children: 'Session started.' })
    // No author name rendered for system
    expect(container.querySelector('.chat-message__author')).not.toBeInTheDocument()
    // No avatar element
    expect(container.querySelector('.chat-message__avatar')).not.toBeInTheDocument()
  })

  it('renders children content', () => {
    renderMessage({ role: 'system', children: 'Roll for initiative!' })
    expect(screen.getByText('Roll for initiative!')).toBeInTheDocument()
  })

  it('does NOT render as dm or player', () => {
    const { container } = renderMessage({ role: 'system', children: 'Notice.' })
    expect(container.querySelector('.chat-message--dm')).not.toBeInTheDocument()
    expect(container.querySelector('.chat-message--player')).not.toBeInTheDocument()
  })
})
