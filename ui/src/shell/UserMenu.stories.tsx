/**
 * UserMenu — the avatar trigger and its popover.
 *
 * Sign-out is the state that matters: only the server can clear an httpOnly
 * cookie, so a refusal has to stay visible and the user has to stay signed in.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { withShell } from '../../.storybook/shellHarness'
import { UserMenu } from './UserMenu'

const meta = {
  title: 'Shell/UserMenu',
  component: UserMenu,
  tags: ['autodocs'],
  parameters: { layout: 'centered' },
  decorators: [withShell()],
} satisfies Meta<typeof UserMenu>

export default meta
type Story = StoryObj<typeof meta>

/** Closed: one button, and `aria-expanded` says so. */
export const Closed: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Open user menu' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
    await expect(canvas.queryByRole('menu')).not.toBeInTheDocument()
  },
}

/**
 * Opened by keyboard alone, and every item reached by keyboard alone.
 *
 * The role row is a DISABLED switch on purpose (x5bz.2 made the role
 * server-authoritative), so the tab order runs trigger → Profile → Sign out.
 */
export const OpenedByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    const trigger = canvas.getByRole('button', { name: 'Open user menu' })
    await expect(trigger).toHaveFocus()

    await userEvent.keyboard('{Enter}')
    await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await expect(canvas.getByRole('menu')).toBeInTheDocument()

    await userEvent.tab()
    await expect(canvas.getByRole('menuitem', { name: 'Profile' })).toHaveFocus()
    await userEvent.tab()
    await expect(canvas.getByRole('menuitem', { name: 'Sign out' })).toHaveFocus()
  },
}

/** The read-only role row, shown as a DM would see it. */
export const OpenAsDungeonMaster: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))
    const roleSwitch = canvas.getByRole('switch', { name: 'Dungeon Master role' })
    await expect(roleSwitch).toBeChecked()
    await expect(roleSwitch).toBeDisabled()
  },
}

export const OpenAsPlayer: Story = {
  decorators: [withShell({ role: 'player' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))
    await expect(canvas.getByRole('switch', { name: 'Dungeon Master role' })).not.toBeChecked()
  },
}

/**
 * The server refused to end the session. The menu STAYS open, the error is
 * announced, and the user is still signed in — the alternative is a UI that
 * says "signed out" while a refresh quietly signs you back in.
 */
export const SignOutRefused: Story = {
  decorators: [withShell({ signOut: async () => false })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))
    await userEvent.click(canvas.getByRole('menuitem', { name: 'Sign out' }))
    const alert = await canvas.findByRole('alert')
    await expect(alert).toHaveTextContent("Couldn't sign out — please try again.")
    await expect(canvas.getByRole('menu')).toBeInTheDocument()
  },
}

/** The happy path: the menu closes once the server has actually cleared it. */
export const SignOutAccepted: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))
    await userEvent.click(canvas.getByRole('menuitem', { name: 'Sign out' }))
    await expect(canvas.queryByRole('menu')).not.toBeInTheDocument()
  },
}

/** A display name long enough to test the initials fallback and the popover width. */
export const LongDisplayName: Story = {
  decorators: [withShell({ displayName: 'Bartholomew Ravensworth-Fitzgerald III' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))
    await expect(canvas.getByRole('menu')).toBeInTheDocument()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))
    await expect(canvas.getByRole('menu')).toBeInTheDocument()
  },
}

export const DarkSignOutRefused: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell({ signOut: async () => false })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))
    await userEvent.click(canvas.getByRole('menuitem', { name: 'Sign out' }))
    await expect(await canvas.findByRole('alert')).toBeVisible()
  },
}
