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

/**
 * agent-forge-harness-27h, rework 1 — the one DELIBERATE VISUAL CHANGE in this
 * branch, pinned so it cannot drift back or drift on.
 *
 * The popover asked for `var(--aether-elevation-2, 0 2px 8px rgba(0,0,0,0.15))`
 * and `--aether-elevation-2` is defined nowhere, so the neutral-black literal
 * is what has shipped — the only floating surface in the app not taking the
 * design system's (warm umber, two-layer) elevation ramp. It now takes
 * `--aether-elevation-raised` = `--md-sys-elevation-level2`, like every other
 * menu surface.
 *
 * The assertion is on the RESOLVED computed value rather than on the token
 * name, because naming a token that does not exist is exactly the bug being
 * fixed: an assertion written against the name would have passed on the old
 * CSS too. Axe has no box-shadow rule and nothing here is screenshot-diffed,
 * so this story is the only thing that can see this change at all.
 */
export const PopoverCarriesTheRaisedElevation: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Open user menu' }))

    const popover = canvasElement.querySelector('.user-menu__popover')
    await expect(popover).toBeInstanceOf(HTMLElement)
    const shadow = getComputedStyle(popover as HTMLElement).boxShadow

    // --md-sys-elevation-level2, as the browser serialises it.
    await expect(shadow).toBe(
      'rgba(58, 34, 12, 0.28) 0px 1px 2px 0px, rgba(58, 34, 12, 0.14) 0px 2px 6px 2px',
    )
    // And explicitly NOT the neutral-black literal that used to ship.
    await expect(shadow).not.toContain('rgba(0, 0, 0')
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
