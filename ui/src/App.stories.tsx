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
import { expectTouchTarget } from '../.storybook/touchTarget'
import {
  atViewport,
  expectNoPageOverflow,
  expectStacked,
  expectViewport,
  expectWorkspaceFits,
  type ViewportName,
} from '../.storybook/viewports'
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

/**
 * Signed in. Navigation resets to Landing whenever the identity changes --
 * except on the FIRST settled observation (agent-forge-harness-y40, R8),
 * which is what lets a cold-loaded deep link survive the session check
 * resolving. `shellHarness` has no "checking" phase of its own (`authStatus`
 * is fixed from the first render), so that first-ever observation is exactly
 * this story's case: explicit here, rather than relying on a reset that must
 * NOT fire for a real cold load either.
 */
export const SignedIn: Story = {
  decorators: [withShell({ authStatus: 'authenticated', screen: 'landing' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: /enter the tavern/i })).toBeInTheDocument()
  },
}

/**
 * The whole entry path by keyboard alone: Tab to the CTA, press Enter, and the
 * tavern replaces the landing screen (30c: the CTA opens it for every signed-in
 * account); then Back to chat, by keyboard too, opens the workspace. Starts on
 * Landing explicitly -- see the SignedIn story's note (agent-forge-harness-y40, R8).
 */
export const EnteredByKeyboard: Story = {
  decorators: [withShell({ authStatus: 'authenticated', screen: 'landing' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    const cta = canvas.getByRole('button', { name: /enter the tavern/i })
    await expect(cta).toHaveFocus()

    await userEvent.keyboard('{Enter}')

    await expect(await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeInTheDocument()
    await expect(canvas.queryByRole('button', { name: /enter the tavern/i })).not.toBeInTheDocument()
    canvas.getByRole('button', { name: 'Back to chat' }).focus()
    await userEvent.keyboard('{Enter}')

    await expect(await canvas.findByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument()
    await expect(canvas.getByText('Ask the Sage…')).toBeInTheDocument()
    await expect(canvas.queryByRole('heading', { name: 'Your Campaigns' })).not.toBeInTheDocument()
  },
}

/**
 * A channel chip is a shortcut straight into that channel. Starts on Landing
 * explicitly -- see the SignedIn story's note (agent-forge-harness-y40, R8).
 */
export const EnteredOnTheGmChannel: Story = {
  decorators: [withShell({ authStatus: 'authenticated', screen: 'landing' })],
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

/**
 * agent-forge-harness-0rn: the session-unavailable screen on a 320px phone.
 * `.auth-screen` is a row flexbox, so its heading, paragraph and button sat
 * side by side; under 600px they stack. "Try again" was an unstyled button
 * below the 44px floor at every width.
 */
async function expectSessionCheckUnavailablePhone(
  canvasElement: HTMLElement,
  viewport: ViewportName,
): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await expectNoPageOverflow()
  const heading = canvas.getByRole('heading', { name: 'Can’t reach the service' })
  const paragraph = canvas.getByText(/We couldn’t check your session/)
  const retry = canvas.getByRole('button', { name: 'Try again' })
  await expectStacked([heading, paragraph, retry])
  await expectTouchTarget(canvas, 'Try again')
}

export const SessionCheckUnavailablePhone320: Story = {
  ...atViewport('phone320'),
  decorators: [withShell({ authStatus: 'unavailable', retryAuthCheck: fn() })],
  play: async ({ canvasElement }) => expectSessionCheckUnavailablePhone(canvasElement, 'phone320'),
}

/** agent-forge-harness-zh9 L-1: AC-1's own width list (320/375/390/599) had
 * no session-unavailable story at 375, 390 or 599. */
export const SessionCheckUnavailablePhone375: Story = {
  ...atViewport('phone375'),
  decorators: [withShell({ authStatus: 'unavailable', retryAuthCheck: fn() })],
  play: async ({ canvasElement }) => expectSessionCheckUnavailablePhone(canvasElement, 'phone375'),
}

export const SessionCheckUnavailablePhone390: Story = {
  ...atViewport('phone390'),
  decorators: [withShell({ authStatus: 'unavailable', retryAuthCheck: fn() })],
  play: async ({ canvasElement }) => expectSessionCheckUnavailablePhone(canvasElement, 'phone390'),
}

export const SessionCheckUnavailableEdge599: Story = {
  ...atViewport('edge599'),
  decorators: [withShell({ authStatus: 'unavailable', retryAuthCheck: fn() })],
  play: async ({ canvasElement }) => expectSessionCheckUnavailablePhone(canvasElement, 'edge599'),
}

/** agent-forge-harness-0rn: the session-check hold on a 320px phone. A
 * regression pin for AC-1's list of states: one short line, which fits with
 * or without the phone rule. */
export const CheckingTheSessionPhone320: Story = {
  ...atViewport('phone320'),
  decorators: [withShell({ authStatus: 'checking' })],
  play: async ({ canvasElement }) => {
    await expectViewport('phone320')
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent('Loading…')
    await expectNoPageOverflow()
    const box = canvas.getByRole('status').getBoundingClientRect()
    await expect(box.left).toBeGreaterThanOrEqual(0)
    await expect(box.right).toBeLessThanOrEqual(window.innerWidth)
  },
}

/**
 * agent-forge-harness-0rn: the phone sign-in path, composed. App imports every
 * shell stylesheet, so this is the one local story where all of the shell's
 * CSS runs together: Landing, then the workspace, then the drawer, with no
 * page-level overflow at any step, and (where the page check is blind, inside
 * the workspace's clipping boxes) nothing clipped and the composer and "Send
 * message" on screen. (The e2e spec `phone.spec.ts` runs the same path
 * against the real stack in CI.)
 */
export const SignedInPhone390: Story = {
  ...atViewport('phone390'),
  decorators: [withShell({ authStatus: 'authenticated', screen: 'landing' })],
  play: async ({ canvasElement }) => {
    await expectViewport('phone390')
    const canvas = within(canvasElement)
    await expectNoPageOverflow()
    await userEvent.click(canvas.getByRole('button', { name: /enter the tavern/i }))
    // 30c: the CTA opens the tavern; it must fit the phone too, then Back to chat enters the workspace.
    await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })
    await expectNoPageOverflow()
    await userEvent.click(canvas.getByRole('button', { name: 'Back to chat' }))
    const menu = await canvas.findByRole('button', { name: 'Open navigation' })
    await expectNoPageOverflow()
    await expectWorkspaceFits(canvasElement)
    await userEvent.click(menu)
    const drawer = canvas.getByRole('dialog', { name: 'Navigation' })
    await expectNoPageOverflow()
    await expectWorkspaceFits(canvasElement)
    await userEvent.click(within(drawer).getByRole('button', { name: 'New conversation' }))
    await expect(canvas.queryByRole('dialog', { name: 'Navigation' })).toBeNull()
    await expectNoPageOverflow()
    await expectWorkspaceFits(canvasElement)
  },
}
