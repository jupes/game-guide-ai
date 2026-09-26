/**
 * Landing — the entry screen. The one thing worth pinning is the role gate:
 * the GM channel is DM-only, and a player must not see a chip for it.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { withShell } from '../../.storybook/shellHarness'
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
