import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import * as React from 'react'

import { menuOptions, toolAvailability } from './registry'
import { SlashMenu } from './SlashMenu'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

/**
 * The composer the menu belongs to, reduced to its ARIA.
 *
 * The menu's own props say what this is for — `id` is documented as "the
 * composer's `aria-controls` target" and `optionId` feeds
 * `aria-activedescendant` — so a story that renders the listbox on its own is
 * rendering half a widget: a popup with nothing declaring that it is a popup,
 * or that it is open. This mirrors ToolComposer's real markup (the role sits on
 * a wrapper because `role="combobox"` is not allowed on `<textarea>`), which is
 * also what lets axe judge the listbox as the combobox popup it is.
 */
function ComboboxHost({ children }: { children: React.ReactNode }): React.JSX.Element {
  return (
    <div>
      {children}
      <div role="combobox" aria-label="Message" aria-expanded aria-haspopup="listbox" aria-controls="slash-menu">
        <textarea aria-label="Message" defaultValue="/" rows={1} />
      </div>
    </div>
  )
}

const meta = {
  title: 'GM/SlashMenu',
  component: SlashMenu,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  decorators: [(Story: React.ComponentType) => <ComboboxHost><Story /></ComboboxHost>],
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
