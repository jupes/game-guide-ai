import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { REGISTRY, overflow, toolAvailability } from './registry'
import { ToolMenu } from './ToolMenu'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })
const UNPINNED = overflow(REGISTRY.default_pinned)

const meta = {
  title: 'GM/ToolMenu',
  component: ToolMenu,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    tools: UNPINNED,
    availability: ALL_ENABLED,
    onArm: fn(),
    onCustomise: fn(),
  },
} satisfies Meta<typeof ToolMenu>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/** SLASH-13: opening moves focus to the first item; Escape returns it. */
export const KeyboardContract: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const button = canvas.getByRole('button', { name: /more/i })
    await userEvent.click(button)
    const items = canvas.getAllByRole('menuitem')
    await expect(items[0]).toHaveFocus()
    await userEvent.keyboard('{ArrowDown}')
    await expect(items[1]).toHaveFocus()
    await userEvent.keyboard('{End}')
    await expect(items[items.length - 1]).toHaveFocus()
    await userEvent.keyboard('{Escape}')
    await expect(canvas.queryByRole('menu')).not.toBeInTheDocument()
    await expect(button).toHaveFocus()
  },
}

/** SLASH-13: a pick arms and leaves focus for the composer, not the button. */
export const PickArms: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: /more/i }))
    await userEvent.click(canvas.getByRole('menuitem', { name: new RegExp(UNPINNED[0].blurb, 'i') }))
    await expect(args.onArm).toHaveBeenCalledWith(expect.objectContaining({ id: UNPINNED[0].id }))
    await expect(canvas.getByRole('button', { name: /more/i })).not.toHaveFocus()
  },
}

/** AE-12: a disabled tool is listed with its reason and arms nothing. */
export const DisabledTool: Story = {
  args: { availability: DEFAULTS },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: /more/i }))
    const portrait = canvas.getByRole('menuitem', { name: /portrait or scene art/i })
    await expect(portrait).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(portrait)
    await expect(args.onArm).not.toHaveBeenCalled()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}
