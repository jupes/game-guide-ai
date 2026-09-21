/**
 * AppHeader — the channel switcher band, with the model picker and the theme
 * control anchored at its right edge.
 *
 * The ModelPicker inside it takes no props here, so it reaches for `/models`
 * over the real `fetch`. The stub is installed as a CSF `beforeEach`, which
 * runs before the first render — an effect-based install would land after the
 * request had already gone out.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { json, stubFetch, withShell } from '../../.storybook/shellHarness'
import { AppHeader } from './AppHeader'

const CATALOG = {
  default: 'auto',
  models: [
    { id: 'auto', display_name: 'Automatic' },
    { id: 'sonnet', display_name: 'Sonnet — balanced' },
  ],
}

const meta = {
  title: 'Shell/AppHeader',
  component: AppHeader,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  beforeEach: stubFetch(() => json(CATALOG)),
  decorators: [withShell({ conversations: [{ mode: 'sage' }], selected: 0 })],
} satisfies Meta<typeof AppHeader>

export default meta
type Story = StoryObj<typeof meta>

/** A DM: four channels, Sage selected. */
export const DungeonMaster: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Sage' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await expect(canvas.getByRole('button', { name: 'GM' })).toBeInTheDocument()
  },
}

/** A player: three channels, and the GM channel is absent rather than disabled. */
export const Player: Story = {
  decorators: [
    withShell({ role: 'player', conversations: [{ mode: 'sage' }], selected: 0 }),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'GM' })).not.toBeInTheDocument()
  },
}

/** The GM channel selected, so its ember accent is on screen. */
export const GmChannelSelected: Story = {
  decorators: [withShell({ mode: 'gm', conversations: [{ mode: 'gm' }], selected: 0 })],
}

/**
 * Switching channel by keyboard alone: Tab across the chips, press Enter, and
 * the pressed state moves.
 */
export const ChannelSwitchedByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    await expect(canvas.getByRole('button', { name: 'Sage' })).toHaveFocus()
    await userEvent.tab()
    await userEvent.tab()
    const rules = canvas.getByRole('button', { name: 'Rules' })
    await expect(rules).toHaveFocus()

    await userEvent.keyboard('{Enter}')
    await expect(rules).toHaveAttribute('aria-pressed', 'true')
    await expect(canvas.getByRole('button', { name: 'Sage' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  },
}

/** The theme switch is a real switch, reachable and operable from the keyboard. */
export const ThemeToggledByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const themeSwitch = canvas.getByRole('switch', { name: 'Dark theme' })
    await expect(themeSwitch).not.toBeChecked()
    themeSwitch.focus()
    await userEvent.keyboard(' ')
    await expect(themeSwitch).toBeChecked()
    await expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  },
}

/** No conversation open: the model picker has nothing to bind to. */
export const NoConversationOpen: Story = {
  decorators: [withShell()],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('combobox', { name: 'Model' })).toBeDisabled()
  },
}

/** `/models` is down: the picker falls back rather than rendering an empty menu. */
export const ModelCatalogUnavailable: Story = {
  beforeEach: stubFetch(() => json({ detail: 'unavailable' }, 503)),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const select = canvas.getByRole('combobox', { name: 'Model' })
    await expect(within(select).getAllByRole('option')).toHaveLength(1)
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('switch', { name: 'Dark theme' })).toBeChecked()
  },
}

export const DarkGmChannel: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell({ mode: 'gm', conversations: [{ mode: 'gm' }], selected: 0 })],
}
