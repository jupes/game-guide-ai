/**
 * CustomiseRailDialog (1kg.3.3) — SLASH-15, RAIL-10, RAIL-11, AE-11, LAYOUT-10.
 */

import { describe, it, expect, vi } from 'vitest'
import * as React from 'react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { REGISTRY, RAIL_LIMIT, toolAvailability } from './registry'
import { CustomiseRailDialog, RAIL_FULL_HINT } from './CustomiseRailDialog'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

function renderDialog(overrides: Partial<React.ComponentProps<typeof CustomiseRailDialog>> = {}) {
  const props: React.ComponentProps<typeof CustomiseRailDialog> = {
    open: true,
    pins: REGISTRY.default_pinned,
    availability: ALL_ENABLED,
    onSave: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  }
  return { props, ...render(<CustomiseRailDialog {...props} />) }
}

/** The tools as the dialog orders them: pinned first, in rail order. */
function rowLabels() {
  return within(screen.getByRole('dialog'))
    .getAllByRole('switch')
    .map((toggle) => toggle.getAttribute('aria-label'))
}

describe('CustomiseRailDialog — the list (SLASH-15)', () => {
  it('lists every tool, pinned first in rail order', () => {
    renderDialog({ pins: ['rules', 'npc'] })
    expect(rowLabels()).toEqual([
      'Pin Rules',
      'Pin NPC',
      ...REGISTRY.tools.filter((t) => t.id !== 'rules' && t.id !== 'npc').map((t) => `Pin ${t.label}`),
    ])
  })

  it('shows which tools are pinned', () => {
    renderDialog({ pins: ['rules'] })
    expect(screen.getByRole('switch', { name: 'Pin Rules' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('switch', { name: 'Pin Recap' })).toHaveAttribute('aria-checked', 'false')
  })

  it('renders nothing while closed', () => {
    renderDialog({ open: false })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('CustomiseRailDialog — the five-pin rule (AE-11, RAIL-11)', () => {
  it('disables every unpinned toggle at five, with the explanation', () => {
    renderDialog({ pins: REGISTRY.default_pinned })
    expect(REGISTRY.default_pinned).toHaveLength(RAIL_LIMIT)
    const recap = screen.getByRole('switch', { name: 'Pin Recap' })
    expect(recap).toHaveAttribute('aria-disabled', 'true')
    expect(recap).toHaveAccessibleDescription(RAIL_FULL_HINT)
  })

  it('refuses the sixth pin even when it is tapped', async () => {
    renderDialog({ pins: REGISTRY.default_pinned })
    await userEvent.click(screen.getByRole('switch', { name: 'Pin Recap' }))
    expect(screen.getByRole('switch', { name: 'Pin Recap' })).toHaveAttribute('aria-checked', 'false')
  })

  it('AE-11: a disabled pinned tool can still be unpinned', async () => {
    renderDialog({ pins: ['npc', 'portrait'], availability: DEFAULTS })
    const portrait = screen.getByRole('switch', { name: 'Pin Portrait' })
    expect(portrait).toHaveAttribute('aria-checked', 'true')
    expect(portrait).not.toHaveAttribute('aria-disabled')
    await userEvent.click(portrait)
    expect(screen.getByRole('switch', { name: 'Pin Portrait' })).toHaveAttribute('aria-checked', 'false')
  })

  it('refuses to pin a disabled tool, and says why (RAIL-10)', async () => {
    renderDialog({ pins: ['npc'], availability: DEFAULTS })
    const portrait = screen.getByRole('switch', { name: 'Pin Portrait' })
    expect(portrait).toHaveAttribute('aria-disabled', 'true')
    expect(portrait).toHaveAccessibleDescription("Image generation isn't set up yet.")
    await userEvent.click(portrait)
    expect(screen.getByRole('switch', { name: 'Pin Portrait' })).toHaveAttribute('aria-checked', 'false')
  })

  it('frees a slot as soon as something is unpinned', async () => {
    renderDialog({ pins: REGISTRY.default_pinned })
    await userEvent.click(screen.getByRole('switch', { name: 'Pin Rules' }))
    expect(screen.getByRole('switch', { name: 'Pin Recap' })).not.toHaveAttribute('aria-disabled')
  })
})

describe('CustomiseRailDialog — order (SLASH-15, never drag-only)', () => {
  it('moves a pin up and down with buttons', async () => {
    renderDialog({ pins: ['npc', 'monster', 'loot'] })
    await userEvent.click(screen.getByRole('button', { name: 'Move Monster up' }))
    expect(rowLabels().slice(0, 3)).toEqual(['Pin Monster', 'Pin NPC', 'Pin Loot'])
    await userEvent.click(screen.getByRole('button', { name: 'Move Monster down' }))
    expect(rowLabels().slice(0, 3)).toEqual(['Pin NPC', 'Pin Monster', 'Pin Loot'])
  })

  it('disables the move that would fall off either end', () => {
    renderDialog({ pins: ['npc', 'monster'] })
    expect(screen.getByRole('button', { name: 'Move NPC up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move Monster down' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move NPC down' })).toBeEnabled()
  })

  it('offers no move at all for a tool that is not pinned', () => {
    renderDialog({ pins: ['npc'] })
    expect(screen.getByRole('button', { name: 'Move Recap up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move Recap down' })).toBeDisabled()
  })
})

describe('CustomiseRailDialog — committing (SLASH-15)', () => {
  it('changes nothing until Save', async () => {
    const { props } = renderDialog({ pins: ['npc'] })
    await userEvent.click(screen.getByRole('switch', { name: 'Pin Recap' }))
    expect(props.onSave).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(props.onSave).toHaveBeenCalledWith(['npc', 'recap'])
  })

  it('Cancel reports a cancel and saves nothing', async () => {
    const { props } = renderDialog({ pins: ['npc'] })
    await userEvent.click(screen.getByRole('switch', { name: 'Pin Recap' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(props.onCancel).toHaveBeenCalledTimes(1)
    expect(props.onSave).not.toHaveBeenCalled()
  })

  it('Escape cancels', async () => {
    const { props } = renderDialog()
    await userEvent.keyboard('{Escape}')
    expect(props.onCancel).toHaveBeenCalledTimes(1)
    expect(props.onSave).not.toHaveBeenCalled()
  })

  it('a click on the scrim cancels', async () => {
    const { props } = renderDialog()
    await userEvent.click(document.querySelector('.gm-customise__scrim')!)
    expect(props.onCancel).toHaveBeenCalledTimes(1)
  })

  it('a press inside the dialog does not', async () => {
    const { props } = renderDialog()
    await userEvent.click(screen.getByRole('dialog'))
    expect(props.onCancel).not.toHaveBeenCalled()
  })

  it('Reset to defaults restores the registry defaults, still only on Save', async () => {
    const { props } = renderDialog({ pins: ['recap'] })
    await userEvent.click(screen.getByRole('button', { name: /reset to defaults/i }))
    expect(props.onSave).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(props.onSave).toHaveBeenCalledWith([...REGISTRY.default_pinned])
  })

  it('re-opening starts from the real pins, never from an abandoned edit', async () => {
    const { rerender } = renderDialog({ pins: ['npc'] })
    await userEvent.click(screen.getByRole('switch', { name: 'Pin Recap' }))
    rerender(<CustomiseRailDialog open={false} pins={['npc']} availability={ALL_ENABLED} onSave={vi.fn()} onCancel={vi.fn()} />)
    rerender(<CustomiseRailDialog open pins={['npc']} availability={ALL_ENABLED} onSave={vi.fn()} onCancel={vi.fn()} />)
    expect(screen.getByRole('switch', { name: 'Pin Recap' })).toHaveAttribute('aria-checked', 'false')
  })
})

describe('CustomiseRailDialog — dialog semantics (LAYOUT-10)', () => {
  it('is a modal dialog with a name', () => {
    renderDialog()
    const dialog = screen.getByRole('dialog', { name: 'Customise rail' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('takes focus when it opens', () => {
    renderDialog()
    expect(screen.getByRole('dialog')).toHaveFocus()
  })

  it('returns focus to whatever opened it', async () => {
    function Host() {
      const [open, setOpen] = React.useState(false)
      return (
        <div>
          <button type="button" onClick={() => setOpen(true)}>
            More
          </button>
          <CustomiseRailDialog
            open={open}
            pins={['npc']}
            availability={ALL_ENABLED}
            onSave={() => setOpen(false)}
            onCancel={() => setOpen(false)}
          />
        </div>
      )
    }
    render(<Host />)
    const opener = screen.getByRole('button', { name: 'More' })
    await userEvent.click(opener)
    expect(screen.getByRole('dialog')).toHaveFocus()
    await userEvent.keyboard('{Escape}')
    expect(opener).toHaveFocus()
  })

  it('traps Tab inside the dialog', async () => {
    renderDialog({ pins: ['npc'] })
    const dialog = screen.getByRole('dialog')
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled])'))
    const last = focusable[focusable.length - 1]
    last.focus()
    await userEvent.tab()
    expect(dialog.contains(document.activeElement)).toBe(true)
  })

  it('wraps backwards from the dialog itself to the last control', async () => {
    renderDialog({ pins: ['npc'] })
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveFocus()
    await userEvent.tab({ shift: true })
    expect(dialog.contains(document.activeElement)).toBe(true)
  })
})
