import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { REGISTRY, toolAvailability } from './registry'
import { ToolRail } from './ToolRail'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

const meta = {
  title: 'GM/ToolRail',
  component: ToolRail,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    pins: [...REGISTRY.default_pinned],
    availability: ALL_ENABLED,
    onArm: fn(),
    onCustomise: fn(),
  },
} satisfies Meta<typeof ToolRail>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/** RAIL-1: a tap arms the composer. There is no prop that could run anything. */
export const TapArms: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: /npc/i }))
    await expect(args.onArm).toHaveBeenCalledWith(expect.objectContaining({ id: 'npc' }))
  },
}

/** RAIL-10: a pin whose capability is off keeps its place and says why. */
export const DisabledPin: Story = {
  args: { pins: ['npc', 'portrait', 'loot'], availability: DEFAULTS },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const portrait = canvas.getByRole('button', { name: /portrait/i })
    await expect(portrait).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(portrait)
    await expect(args.onArm).not.toHaveBeenCalled()
  },
}

/** §12.2: no pins is a legitimate state — More and the hint. */
export const NoPins: Story = {
  args: { pins: [] },
}

/** RAIL-13: without a campaign the rail is inert and says what to do. */
export const NoCampaign: Story = {
  args: { campaignSelected: false, onChooseCampaign: fn() },
}

/** AE-58: the capability lookup failed. Every tool is disabled, with Retry. */
export const CapabilityLookupFailed: Story = {
  args: { availability: toolAvailability(null), capabilitiesFailed: true, onRetryCapabilities: fn() },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}
