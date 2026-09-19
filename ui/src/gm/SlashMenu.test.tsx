/**
 * SlashMenu (1kg.3.3) — SLASH-9, SLASH-10, SLASH-12.
 *
 * The roles are the point: a `listbox` of non-focusable `option`s, so focus can
 * stay in the composer. The handoff rendered buttons, which would have moved
 * focus on every arrow key.
 */

import { describe, it, expect, vi } from 'vitest'
import type * as React from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { REGISTRY, menuOptions, toolAvailability } from './registry'
import { SlashMenu } from './SlashMenu'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

function renderMenu(overrides: Partial<React.ComponentProps<typeof SlashMenu>> = {}) {
  const props: React.ComponentProps<typeof SlashMenu> = {
    id: 'menu',
    token: '',
    options: menuOptions(''),
    availability: ALL_ENABLED,
    activeIndex: 0,
    optionId: (index) => `option-${index}`,
    onPick: vi.fn(),
    onHover: vi.fn(),
    ...overrides,
  }
  return { props, ...render(<SlashMenu {...props} />) }
}

describe('SlashMenu — roles (SLASH-12)', () => {
  it('is a listbox of options, not a list of buttons', () => {
    renderMenu()
    const listbox = screen.getByRole('listbox', { name: 'Tools' })
    expect(listbox.tagName).toBe('UL')
    expect(screen.getAllByRole('option')).toHaveLength(REGISTRY.tools.length)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('leaves every option out of the tab order, so focus stays in the composer', () => {
    renderMenu()
    for (const option of screen.getAllByRole('option')) {
      expect(option).not.toHaveAttribute('tabindex')
    }
  })

  it('carries the id the composer points `aria-controls` at', () => {
    renderMenu({ id: 'composer-listbox' })
    expect(screen.getByRole('listbox')).toHaveAttribute('id', 'composer-listbox')
  })

  it('gives each option the id the composer names in `aria-activedescendant`', () => {
    renderMenu({ optionId: (index) => `opt-${index}` })
    expect(screen.getAllByRole('option')[0]).toHaveAttribute('id', 'opt-0')
  })
})

describe('SlashMenu — options (SLASH-9)', () => {
  it('lists every registry tool, in registry order, for a bare slash', () => {
    renderMenu()
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(
      REGISTRY.tools.map((tool) => expect.stringContaining(tool.command)),
    )
  })

  it('marks the active option selected, and only that one', () => {
    renderMenu({ activeIndex: 2 })
    const options = screen.getAllByRole('option')
    expect(options[2]).toHaveAttribute('aria-selected', 'true')
    expect(options.filter((o) => o.getAttribute('aria-selected') === 'true')).toHaveLength(1)
  })

  it('renders one inert row on zero matches instead of vanishing', () => {
    renderMenu({ token: 'xyz', options: [], activeIndex: -1 })
    const row = screen.getByRole('option')
    expect(row).toHaveTextContent('No tool matches "/xyz"')
    expect(row).toHaveAttribute('aria-disabled', 'true')
  })

  it('picks a tool on click', async () => {
    const onPick = vi.fn()
    renderMenu({ token: 'mo', options: menuOptions('mo'), onPick })
    await userEvent.click(screen.getByRole('option', { name: /monster/i }))
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: 'monster' }))
  })

  it('moves the active option on hover (§4.4)', async () => {
    const onHover = vi.fn()
    renderMenu({ onHover })
    await userEvent.hover(screen.getAllByRole('option')[3])
    expect(onHover).toHaveBeenCalledWith(3)
  })

  it('prevents mousedown so the composer keeps focus (§4.4)', () => {
    renderMenu()
    const event = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    fireEvent(screen.getByRole('option', { name: /npc/i }), event)
    expect(event.defaultPrevented).toBe(true)
  })
})

describe('SlashMenu — disabled tools (SLASH-10, RAIL-10)', () => {
  it('lists a capability-disabled tool with its reason', () => {
    renderMenu({ availability: DEFAULTS })
    const portrait = screen.getByRole('option', { name: /portrait/i })
    expect(portrait).toHaveAttribute('aria-disabled', 'true')
    expect(portrait).toHaveTextContent("Image generation isn't set up yet.")
  })

  it('cannot be picked', async () => {
    const onPick = vi.fn()
    renderMenu({ availability: DEFAULTS, onPick })
    await userEvent.click(screen.getByRole('option', { name: /portrait/i }))
    expect(onPick).not.toHaveBeenCalled()
  })

  it('can still be the active option, so a screen reader reaches the reason', () => {
    const portraitIndex = REGISTRY.tools.findIndex((tool) => tool.id === 'portrait')
    renderMenu({ availability: DEFAULTS, activeIndex: portraitIndex })
    expect(screen.getByRole('option', { name: /portrait/i })).toHaveAttribute('aria-selected', 'true')
  })

  it('does not offer the ↵ affordance on a row that cannot be accepted', () => {
    const portraitIndex = REGISTRY.tools.findIndex((tool) => tool.id === 'portrait')
    renderMenu({ availability: DEFAULTS, activeIndex: portraitIndex })
    expect(screen.getByRole('option', { name: /portrait/i }).textContent).not.toContain('↵')
  })
})
