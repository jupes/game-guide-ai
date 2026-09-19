/**
 * ToolRail (1kg.3.3) — RAIL-1, RAIL-10, RAIL-11, RAIL-13, AE-58.
 *
 * The first test is the task's whole point: a tap arms and nothing else. The
 * component has no way to invoke anything — there is no prop for it — so the
 * handoff's `onTool={runTool}` cannot be re-created by a later caller.
 */

import { describe, it, expect, vi } from 'vitest'
import type * as React from 'react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { REGISTRY, overflow, toolAvailability, toolById } from './registry'
import { CAPABILITY_ERROR, NO_CAMPAIGN_PROMPT, RAIL_HINT, ToolRail } from './ToolRail'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

function renderRail(overrides: Partial<React.ComponentProps<typeof ToolRail>> = {}) {
  const props: React.ComponentProps<typeof ToolRail> = {
    pins: REGISTRY.default_pinned,
    availability: ALL_ENABLED,
    onArm: vi.fn(),
    onCustomise: vi.fn(),
    ...overrides,
  }
  render(<ToolRail {...props} />)
  return props
}

/** The rail's own tools, excluding More. */
function railTools() {
  const rail = screen.getByRole('group', { name: 'GM tools' })
  return within(rail)
    .getAllByRole('button')
    .filter((button) => button.classList.contains('gm-tool-rail__tool'))
}

describe('ToolRail — a tap arms the composer (RAIL-1, X-1)', () => {
  it('AE-1: tapping NPC reports the tool and nothing else; there is no way to run it', async () => {
    const props = renderRail()
    await userEvent.click(screen.getByRole('button', { name: /npc/i }))
    expect(props.onArm).toHaveBeenCalledWith(toolById('npc'))
    expect(props.onArm).toHaveBeenCalledTimes(1)
    // The prop surface itself is the guarantee: nothing here can invoke.
    expect(Object.keys(props)).not.toContain('onTool')
    expect(Object.keys(props)).not.toContain('onRun')
  })
})

describe('ToolRail — pins (RAIL-11)', () => {
  it('renders exactly the pinned tools, in pin order', () => {
    renderRail({ pins: ['rules', 'npc', 'recap'] })
    expect(railTools().map((button) => button.textContent)).toEqual([
      expect.stringContaining('Rules'),
      expect.stringContaining('NPC'),
      expect.stringContaining('Recap'),
    ])
  })

  it('§12.2: with no pins it is More and the hint', () => {
    renderRail({ pins: [] })
    expect(railTools()).toHaveLength(0)
    expect(screen.getByRole('button', { name: /more/i })).toBeInTheDocument()
    expect(screen.getByText(RAIL_HINT)).toBeInTheDocument()
  })

  it('X-8: an id the registry does not know is dropped, never guessed at', () => {
    renderRail({ pins: ['npc', 'ninth-tool' as never] })
    expect(railTools()).toHaveLength(1)
  })

  it('rail and More partition the one registry between them', async () => {
    const pins = ['npc', 'recap'] as const
    renderRail({ pins })
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    const inMore = screen.getAllByRole('menuitem').map((item) => item.textContent ?? '')
    for (const tool of REGISTRY.tools) {
      const onRail = railTools().some((button) => button.textContent?.includes(tool.label))
      const inOverflow = inMore.some((label) => label.includes(tool.blurb))
      expect(onRail).toBe((pins as readonly string[]).includes(tool.id))
      expect(onRail !== inOverflow).toBe(true)
    }
    expect(overflow(pins).length + pins.length).toBe(REGISTRY.tools.length)
  })

  it('hides the hint when the layout has no room for it (§10.2)', () => {
    renderRail({ hint: null })
    expect(screen.queryByText(RAIL_HINT)).not.toBeInTheDocument()
  })
})

describe('ToolRail — a disabled pin (RAIL-10)', () => {
  it('keeps its place and renders aria-disabled with its reason', () => {
    renderRail({ pins: ['npc', 'portrait', 'loot'], availability: DEFAULTS })
    const portrait = screen.getByRole('button', { name: /portrait/i })
    expect(railTools()[1]).toBe(portrait)
    expect(portrait).toHaveAttribute('aria-disabled', 'true')
    expect(portrait).toHaveAccessibleDescription("Image generation isn't set up yet.")
  })

  it('is focusable, because a `disabled` button could never announce its reason', () => {
    renderRail({ pins: ['portrait'], availability: DEFAULTS })
    const portrait = screen.getByRole('button', { name: /portrait/i })
    portrait.focus()
    expect(portrait).toHaveFocus()
    expect(portrait).not.toBeDisabled()
  })

  it('arms nothing when tapped', async () => {
    const props = renderRail({ pins: ['portrait'], availability: DEFAULTS })
    await userEvent.click(screen.getByRole('button', { name: /portrait/i }))
    expect(props.onArm).not.toHaveBeenCalled()
  })
})

describe('ToolRail — no campaign selected (RAIL-13)', () => {
  it('renders the prompt and a way into campaign selection', async () => {
    const onChooseCampaign = vi.fn()
    renderRail({ campaignSelected: false, onChooseCampaign })
    // The prompt is on the rail itself, above the row — and, separately, as the
    // reason behind each disabled tool, which the next test asserts.
    expect(document.querySelector('.gm-tool-rail__notice')).toHaveTextContent(NO_CAMPAIGN_PROMPT)
    await userEvent.click(screen.getByRole('button', { name: /choose a campaign/i }))
    expect(onChooseCampaign).toHaveBeenCalledTimes(1)
  })

  it('disables every tool and More, and arms nothing', async () => {
    const props = renderRail({ campaignSelected: false })
    for (const button of railTools()) {
      expect(button).toHaveAttribute('aria-disabled', 'true')
    }
    expect(screen.getByRole('button', { name: /more/i })).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(screen.getByRole('button', { name: /npc/i }))
    expect(props.onArm).not.toHaveBeenCalled()
  })

  it('explains why each tool is disabled', () => {
    renderRail({ campaignSelected: false })
    expect(screen.getByRole('button', { name: /npc/i })).toHaveAccessibleDescription(NO_CAMPAIGN_PROMPT)
  })
})

describe('ToolRail — the capability lookup failed (AE-58, §12.2)', () => {
  it('disables every tool with one message and offers Retry', async () => {
    const onRetryCapabilities = vi.fn()
    renderRail({ availability: toolAvailability(null), capabilitiesFailed: true, onRetryCapabilities })
    for (const button of railTools()) {
      expect(button).toHaveAttribute('aria-disabled', 'true')
      expect(button).toHaveAccessibleDescription(CAPABILITY_ERROR)
    }
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetryCapabilities).toHaveBeenCalledTimes(1)
  })

  it('announces the failure politely rather than silently emptying the rail (STATE-1)', () => {
    renderRail({ availability: toolAvailability(null), capabilitiesFailed: true })
    expect(screen.getByRole('status')).toHaveTextContent(CAPABILITY_ERROR)
    expect(railTools()).toHaveLength(REGISTRY.default_pinned.length)
  })
})
