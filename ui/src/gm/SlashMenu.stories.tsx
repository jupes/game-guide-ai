import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { menuOptions, toolAvailability } from './registry'
import { SlashMenu } from './SlashMenu'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

const meta = {
  title: 'GM/SlashMenu',
  component: SlashMenu,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    id: 'slash-menu',
    token: '',
    options: menuOptions(''),
    availability: ALL_ENABLED,
    activeIndex: 0,
    optionId: (index: number) => `slash-option-${index}`,
    onPick: fn(),
    onHover: fn(),
  },
} satisfies Meta<typeof SlashMenu>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

export const Filtered: Story = {
  args: { token: 'mo', options: menuOptions('mo') },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('option', { name: /monster/i }))
    await expect(args.onPick).toHaveBeenCalledWith(expect.objectContaining({ id: 'monster' }))
  },
}

/** SLASH-9: zero matches render one inert row, never an empty menu. */
export const NoMatch: Story = {
  args: { token: 'xyz', options: [], activeIndex: -1 },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('option')).toHaveTextContent('No tool matches "/xyz"')
  },
}

/** SLASH-10: a disabled tool is listed with its reason and cannot be picked. */
export const DisabledTool: Story = {
  args: { token: 'p', options: menuOptions('p'), availability: DEFAULTS },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const portrait = canvas.getByRole('option', { name: /portrait/i })
    await expect(portrait).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(portrait)
    await expect(args.onPick).not.toHaveBeenCalled()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}
