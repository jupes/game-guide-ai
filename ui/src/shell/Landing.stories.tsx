/**
 * Landing — the entry screen. The one thing worth pinning is the role gate:
 * the GM channel is DM-only, and a player must not see a chip for it.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { withShell } from '../../.storybook/shellHarness'
import { atViewport, expectLeftEdge, expectNoPageOverflow, expectSpans, expectStacked, expectViewport, type ViewportName } from '../../.storybook/viewports'
import { Landing } from './Landing'

const meta = {
  title: 'Shell/Landing',
  component: Landing,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  decorators: [withShell({ screen: 'landing' })],
} satisfies Meta<typeof Landing>

export default meta
type Story = StoryObj<typeof meta>

/** A DM sees all four channels. */
export const DungeonMaster: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: /enter the tavern/i })).toBeInTheDocument()
    for (const label of ['Sage', 'Spell', 'Rules', 'GM']) {
      await expect(canvas.getByRole('button', { name: label })).toBeInTheDocument()
    }
  },
}

/** A player sees three: the GM channel is not offered. */
export const Player: Story = {
  decorators: [withShell({ screen: 'landing', role: 'player' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'GM' })).not.toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Sage' })).toBeInTheDocument()
  },
}

/**
 * Keyboard only. Tab reaches the primary CTA first, then each channel chip in
 * the order they are read — no chip is mouse-only, and nothing is skipped.
 */
export const TabOrder: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    await expect(canvas.getByRole('button', { name: /enter the tavern/i })).toHaveFocus()
    for (const label of ['Sage', 'Spell', 'Rules', 'GM']) {
      await userEvent.tab()
      await expect(canvas.getByRole('button', { name: label })).toHaveFocus()
    }
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkPlayer: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell({ screen: 'landing', role: 'player' })],
}

// ── Phone (agent-forge-harness-0rn) ──────────────────────────────────────────
// Under 600px the card's actions — the CTA and every mode chip, each of which
// enters the workspace — stack in one column and span the card, each on the
// 44px floor. At 600px the chips are a row again (the CSS boundary).

async function expectStackedCardActions(
  canvasElement: HTMLElement,
  viewport: ViewportName,
  chips: readonly string[],
): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await expectNoPageOverflow()
  const card = canvasElement.querySelector('.landing__card')
  if (!(card instanceof HTMLElement)) throw new Error('no landing card')
  const actions = [
    canvas.getByRole('button', { name: /enter the tavern/i }),
    ...chips.map((name) => canvas.getByRole('button', { name })),
  ]
  await expectStacked(actions)
  for (const action of actions) {
    await expectSpans(action, card)
    await expect(action.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  }
  // A full-width chip centres its icon and label, as the CTA does.
  for (const name of chips) {
    const chip = canvas.getByRole('button', { name })
    const chipBox = chip.getBoundingClientRect()
    // The icon and the label; the chip's state layer spans it and proves nothing.
    const parts = Array.from(chip.querySelectorAll('.chip__icon, .chip__label'), (part) => part.getBoundingClientRect())
    await expect(parts).toHaveLength(2)
    const contentMid = (Math.min(...parts.map((p) => p.left)) + Math.max(...parts.map((p) => p.right))) / 2
    await expect(Math.abs(contentMid - (chipBox.left + chipBox.right) / 2)).toBeLessThanOrEqual(2)
  }
}

const DM_CHIPS = ['Sage', 'Spell', 'Rules', 'GM']

export const PhoneDm390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => expectStackedCardActions(canvasElement, 'phone390', DM_CHIPS),
}

export const PhoneDm320: Story = {
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => {
    await expectStackedCardActions(canvasElement, 'phone320', DM_CHIPS)
    // The phone gutter, not the desktop's 24px page padding.
    const card = canvasElement.querySelector('.landing__card')
    if (!(card instanceof HTMLElement)) throw new Error('no landing card')
    await expectLeftEdge(card, 16)
  },
}

/** One pixel inside the phone rule: still one column. */
export const Edge599: Story = {
  ...atViewport('edge599'),
  play: async ({ canvasElement }) => expectStackedCardActions(canvasElement, 'edge599', DM_CHIPS),
}

/** At 600px the phone rule no longer applies: the chips share one row. */
export const Edge600: Story = {
  ...atViewport('edge600'),
  play: async ({ canvasElement }) => {
    await expectViewport('edge600')
    const canvas = within(canvasElement)
    await expectNoPageOverflow()
    const tops = DM_CHIPS.map((name) => canvas.getByRole('button', { name }).getBoundingClientRect().top)
    for (const top of tops) {
      await expect(top).toBe(tops[0])
    }
  },
}

export const DarkPhonePlayer390: Story = {
  ...atViewport('phone390', 'dark'),
  decorators: [withShell({ screen: 'landing', role: 'player' })],
  play: async ({ canvasElement }) =>
    expectStackedCardActions(canvasElement, 'phone390', ['Sage', 'Spell', 'Rules']),
}
