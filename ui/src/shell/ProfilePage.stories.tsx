/**
 * ProfilePage — display name and avatar tone, both locally stubbed, plus the
 * read-only role and the list of fields that do not exist yet.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { withShell } from '../../.storybook/shellHarness'
import { ProfilePage } from './ProfilePage'

const meta = {
  title: 'Shell/ProfilePage',
  component: ProfilePage,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  decorators: [withShell({ screen: 'profile' })],
} satisfies Meta<typeof ProfilePage>

export default meta
type Story = StoryObj<typeof meta>

export const DungeonMaster: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('textbox', { name: 'Display name' })).toHaveValue('Alanna Quill')
    const roleSwitch = canvas.getByRole('switch', { name: 'Dungeon Master role' })
    await expect(roleSwitch).toBeChecked()
    await expect(roleSwitch).toBeDisabled()
  },
}

export const Player: Story = {
  decorators: [withShell({ screen: 'profile', role: 'player', displayName: 'Tam Underbough' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('switch', { name: 'Dungeon Master role' })).not.toBeChecked()
  },
}

/**
 * Edited by keyboard alone: Tab into the field, type, and the avatar's initials
 * follow. The tone buttons are `aria-pressed` toggles, so a screen reader
 * hears which colour is selected.
 */
export const RenamedByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = canvas.getByRole('textbox', { name: 'Display name' })
    await userEvent.tab()
    await expect(field).toHaveFocus()

    await userEvent.keyboard('{Control>}a{/Control}Grivvel Oakenshadow')
    await expect(field).toHaveValue('Grivvel Oakenshadow')
    // The header avatar and the four tone swatches all take their initials
    // from the same value, so the rename is visible immediately.
    await expect(canvas.getAllByText('GO')).toHaveLength(5)
  },
}

/** Tone selection, driven from the keyboard, with the pressed state asserted. */
export const TonePickedByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const ember = canvas.getByRole('button', { name: 'Ember avatar' })
    await expect(canvas.getByRole('button', { name: 'Gold avatar' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    ember.focus()
    await userEvent.keyboard('{Enter}')
    await expect(ember).toHaveAttribute('aria-pressed', 'true')
    await expect(canvas.getByRole('button', { name: 'Gold avatar' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  },
}

/** An empty display name — the field allows it, and nothing crashes on initials. */
export const EmptyDisplayName: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = canvas.getByRole('textbox', { name: 'Display name' })
    field.focus()
    await userEvent.keyboard('{Control>}a{/Control}{Backspace}')
    await expect(field).toHaveValue('')
  },
}

/** A name long enough to wrap the header and stress the card's measure. */
export const LongDisplayName: Story = {
  decorators: [
    withShell({
      screen: 'profile',
      displayName: 'Archmagister Seraphina Duskwhisper of the Ninefold Spire',
      avatarTone: 'arcane',
    }),
  ],
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkVerdigris: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell({ screen: 'profile', avatarTone: 'verdigris' })],
}
