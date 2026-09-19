import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { REGISTRY, toolAvailability } from './registry'
import { CustomiseRailDialog, RAIL_FULL_HINT } from './CustomiseRailDialog'
import type { CustomiseRailDialogProps } from './CustomiseRailDialog'
import type { ToolId } from './contracts'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

/** Stateful wrapper, so pinning and reordering actually move in the canvas. */
function LiveDialog(props: CustomiseRailDialogProps) {
  const [pins, setPins] = React.useState<readonly ToolId[]>(props.pins)
  return (
    <CustomiseRailDialog
      {...props}
      pins={pins}
      onSave={(next) => {
        setPins(next)
        props.onSave(next)
      }}
    />
  )
}

const meta = {
  title: 'GM/CustomiseRailDialog',
  component: CustomiseRailDialog,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  args: {
    open: true,
    pins: [...REGISTRY.default_pinned],
    availability: ALL_ENABLED,
    onSave: fn(),
    onCancel: fn(),
  },
  render: (args) => <LiveDialog {...args} />,
} satisfies Meta<typeof CustomiseRailDialog>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/** AE-11: at five pins every unpinned toggle is refused, with the explanation. */
export const RailFull: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const recap = canvas.getByRole('switch', { name: 'Pin Recap' })
    await expect(recap).toHaveAttribute('aria-disabled', 'true')
    await expect(recap).toHaveAccessibleDescription(RAIL_FULL_HINT)
  },
}

/** SLASH-15: reordering is never drag-only. */
export const Reorder: Story = {
  args: { pins: ['npc', 'monster', 'loot'] },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Move Monster up' }))
    const order = canvas.getAllByRole('switch').map((toggle) => toggle.getAttribute('aria-label'))
    await expect(order.slice(0, 3)).toEqual(['Pin Monster', 'Pin NPC', 'Pin Loot'])
  },
}

/** RAIL-10 and AE-11: a disabled tool cannot be pinned, but can be unpinned. */
export const DisabledTools: Story = {
  args: { pins: ['npc', 'portrait'], availability: DEFAULTS },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('switch', { name: 'Pin Map' })).toHaveAttribute('aria-disabled', 'true')
    await expect(canvas.getByRole('switch', { name: 'Pin Portrait' })).not.toHaveAttribute('aria-disabled')
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}
