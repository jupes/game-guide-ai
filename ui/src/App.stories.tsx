/**
 * App — the top-level gate.
 *
 * Its whole job is deciding what a visitor sees, and the four session states it
 * decides from are the reason `authStatus` has four values rather than a
 * boolean: "we do not know who you are" and "you are signed out" are different
 * facts, and only one of them should ever show a sign-in form.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { json, stubFetch, withShell } from '../.storybook/shellHarness'
import App from './App'

const CATALOG = { default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }] }

const workspaceApi = stubFetch((url) => {
  if (url.includes('/models')) return json(CATALOG)
  if (url.includes('/messages')) return json({ conversation_id: 'story', messages: [] })
  if (url.includes('/attachments')) return json({ conversation_id: 'story', attachments: [] })
  return json({ detail: `unrouted: ${url}` }, 404)
})

const meta = {
  title: 'Shell/App',
  component: App,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  beforeEach: workspaceApi,
  decorators: [withShell({ authStatus: 'authenticated' })],
} satisfies Meta<typeof App>

export default meta
type Story = StoryObj<typeof meta>

/**
 * The session check is still in flight. A neutral hold — NOT the workspace
 * (which would scope a conversation to `guest` and strand it when the real
 * identity arrived) and NOT Login (which would flash a sign-in form at someone
 * who is already signed in).
 */
export const CheckingTheSession: Story = {
  decorators: [withShell({ authStatus: 'checking' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent('Loading…')
  },
}

/**
 * The check could not be COMPLETED — a 5xx or a network failure. Not a logout:
 * the cookie may be perfectly valid, and Login would be a dead end because the
 * same backend has to serve it. So: say what happened, and offer a retry.
 */
export const SessionCheckUnavailable: Story = {
  decorators: [withShell({ authStatus: 'unavailable', retryAuthCheck: fn() })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const alert = canvas.getByRole('alert')
    await expect(alert).toHaveTextContent('Can’t reach the service')
    const retry = canvas.getByRole('button', { name: 'Try again' })
    retry.focus()
    await userEvent.keyboard('{Enter}')
  },
}

/** Definitively signed out, with no invite in the URL: Login. */
export const SignedOut: Story = {
  decorators: [withShell({ authStatus: 'unauthenticated' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Sign in to continue')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  },
}

/**
 * Signed out WITH an unspent invite in the fragment: Signup instead.
 *
 * The token travels in the fragment, never the query string, so it stays out of
 * the service's request logs — and App clears it from the address bar as soon
 * as it has read it, which is why the hash is empty again by the time the story
 * has finished rendering.
 */
export const SignedOutWithAnInvite: Story = {
  decorators: [withShell({ authStatus: 'unauthenticated' })],
  beforeEach: () => {
    const original = window.location.hash
    window.location.hash = '#invite=a-one-time-token'
    return () => {
      window.location.hash = original
    }
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(
      canvas.getByText("You've been invited — create your account"),
    ).toBeInTheDocument()
    await expect(window.location.hash).not.toContain('invite=')
  },
}

/** Signed in. Navigation resets to Landing whenever the identity changes. */
export const SignedIn: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: /enter the tavern/i })).toBeInTheDocument()
  },
}

/**
 * The whole entry path by keyboard alone: Tab to the CTA, press Enter, and the
 * workspace replaces the landing screen.
 */
export const EnteredByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    const cta = canvas.getByRole('button', { name: /enter the tavern/i })
    await expect(cta).toHaveFocus()

    await userEvent.keyboard('{Enter}')

    await expect(await canvas.findByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument()
    await expect(canvas.getByText('Ask the Sage…')).toBeInTheDocument()
    await expect(canvas.queryByRole('button', { name: /enter the tavern/i })).not.toBeInTheDocument()
  },
}

/** A channel chip is a shortcut straight into that channel. */
export const EnteredOnTheGmChannel: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const gm = canvas.getByRole('button', { name: 'GM' })
    gm.focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByText('Ask the Game Master…')).toBeInTheDocument()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkSignedOut: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell({ authStatus: 'unauthenticated' })],
}

export const DarkSessionCheckUnavailable: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell({ authStatus: 'unavailable' })],
}
