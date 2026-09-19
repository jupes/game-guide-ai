/**
 * ToolMenu — More (1kg.3.3) — SLASH-13, SLASH-14, RAIL-10, AE-12.
 *
 * The menu pattern's focus contract is the subject here: where focus goes on
 * open, on each arrow, on Escape, on Tab, and — the one that is easy to get
 * wrong — where it goes on a pick, which is the composer and not the button.
 */

import { describe, it, expect, vi } from 'vitest'
import type * as React from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { REGISTRY, overflow, toolAvailability } from './registry'
import { ToolMenu } from './ToolMenu'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })
const UNPINNED = overflow(REGISTRY.default_pinned)

function renderMenu(overrides: Partial<React.ComponentProps<typeof ToolMenu>> = {}) {
  const props: React.ComponentProps<typeof ToolMenu> = {
    tools: UNPINNED,
    availability: ALL_ENABLED,
    onArm: vi.fn(),
    onCustomise: vi.fn(),
    ...overrides,
  }
  render(<ToolMenu {...props} />)
  return { props, button: screen.getByRole('button', { name: /more/i }) }
}

async function open() {
  const rendered = renderMenu()
  await userEvent.click(rendered.button)
  return rendered
}

describe('ToolMenu — the menu button (SLASH-13)', () => {
  it('announces itself as a menu button and reports whether it is open', async () => {
    const { button } = renderMenu()
    expect(button).toHaveAttribute('aria-haspopup', 'menu')
    expect(button).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(button).toHaveAttribute('aria-controls', screen.getByRole('menu').id)
  })

  it('moves focus to the first item on open', async () => {
    await open()
    expect(screen.getAllByRole('menuitem')[0]).toHaveFocus()
  })

  it('closes on a second press', async () => {
    const { button } = await open()
    await userEvent.click(button)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('does not open at all with no campaign selected (RAIL-13)', async () => {
    const { button } = renderMenu({ disabled: true })
    expect(button).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(button)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})

describe('ToolMenu — contents (SLASH-14)', () => {
  it('lists the unpinned tools by blurb, then a separator, then Customise rail…', async () => {
    await open()
    const items = screen.getAllByRole('menuitem').map((item) => item.textContent)
    expect(items).toEqual([...UNPINNED.map((tool) => expect.stringContaining(tool.blurb)), expect.stringContaining('Customise rail')])
    expect(screen.getByRole('separator')).toBeInTheDocument()
  })

  it('reads the same registry as the rail — it lists exactly what is not pinned', async () => {
    renderMenu({ tools: overflow(['npc', 'recap']) })
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    const labels = screen.getAllByRole('menuitem').map((item) => item.textContent ?? '')
    for (const tool of REGISTRY.tools) {
      const listed = labels.some((label) => label.includes(tool.blurb))
      expect(listed).toBe(tool.id !== 'npc' && tool.id !== 'recap')
    }
  })

  it('omits Customise rail… when the host offers no customisation', async () => {
    renderMenu({ onCustomise: undefined })
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    expect(screen.queryByRole('menuitem', { name: /customise/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
  })
})

describe('ToolMenu — keyboard (SLASH-13)', () => {
  it('wraps with the arrow keys', async () => {
    await open()
    const items = screen.getAllByRole('menuitem')
    await userEvent.keyboard('{ArrowDown}')
    expect(items[1]).toHaveFocus()
    await userEvent.keyboard('{ArrowUp}{ArrowUp}')
    expect(items[items.length - 1]).toHaveFocus()
    await userEvent.keyboard('{ArrowDown}')
    expect(items[0]).toHaveFocus()
  })

  it('jumps with Home and End', async () => {
    await open()
    const items = screen.getAllByRole('menuitem')
    await userEvent.keyboard('{End}')
    expect(items[items.length - 1]).toHaveFocus()
    await userEvent.keyboard('{Home}')
    expect(items[0]).toHaveFocus()
  })

  it('activates with Enter', async () => {
    const onArm = vi.fn()
    renderMenu({ onArm })
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    await userEvent.keyboard('{Enter}')
    expect(onArm).toHaveBeenCalledWith(expect.objectContaining({ id: UNPINNED[0].id }))
  })

  it('activates with Space', async () => {
    const onArm = vi.fn()
    renderMenu({ onArm })
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    await userEvent.keyboard(' ')
    expect(onArm).toHaveBeenCalledTimes(1)
  })

  it('closes on Escape and returns focus to the button', async () => {
    const { button } = await open()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(button).toHaveFocus()
  })

  it('closes on Tab and returns focus to the button, so the tab order carries on', async () => {
    const { button } = await open()
    // fireEvent, not userEvent.tab(): the point is what the handler does with
    // the key. user-event moves focus itself afterwards, which is the browser's
    // job and would hide the handler's own focus move.
    fireEvent.keyDown(screen.getAllByRole('menuitem')[0], { key: 'Tab' })
    expect(button).toHaveFocus()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('leaves an unhandled key to the browser', async () => {
    await open()
    await userEvent.keyboard('x')
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('closes on an outside click', async () => {
    await open()
    await userEvent.click(document.body)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})

describe('ToolMenu — picking (SLASH-13, RAIL-1)', () => {
  it('AE-2: a pick arms and leaves focus for the composer to take, not the button', async () => {
    const onArm = vi.fn()
    const { button } = renderMenu({ onArm })
    await userEvent.click(button)
    await userEvent.click(screen.getByRole('menuitem', { name: new RegExp(UNPINNED[0].blurb, 'i') }))
    expect(onArm).toHaveBeenCalledWith(expect.objectContaining({ id: UNPINNED[0].id }))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(button).not.toHaveFocus()
  })

  it('hands focus back to the button before opening Customise, so the dialog can return it', async () => {
    const onCustomise = vi.fn(() => {
      expect(screen.getByRole('button', { name: /more/i })).toHaveFocus()
    })
    const { button } = renderMenu({ onCustomise })
    await userEvent.click(button)
    await userEvent.click(screen.getByRole('menuitem', { name: /customise/i }))
    expect(onCustomise).toHaveBeenCalledTimes(1)
  })

  it('AE-12: a capability-disabled tool is listed with its reason and arms nothing', async () => {
    const onArm = vi.fn()
    renderMenu({ tools: overflow(REGISTRY.default_pinned), availability: DEFAULTS, onArm })
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    const portrait = screen.getByRole('menuitem', { name: /portrait or scene art/i })
    expect(portrait).toHaveAttribute('aria-disabled', 'true')
    expect(portrait).toHaveAccessibleDescription("Image generation isn't set up yet.")
    await userEvent.click(portrait)
    expect(onArm).not.toHaveBeenCalled()
  })

  it('AE-12: a disabled item is still focusable, so its reason can be read aloud', async () => {
    renderMenu({ tools: [REGISTRY.tools.find((tool) => tool.id === 'portrait')!], availability: DEFAULTS })
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    expect(screen.getByRole('menuitem', { name: /portrait or scene art/i })).toHaveFocus()
  })
})
