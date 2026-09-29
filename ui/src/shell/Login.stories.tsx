/**
 * Login — email + password sign-in.
 *
 * The states worth having are the unhappy ones: a submission with nothing
 * filled in, a submission the server rejects, and the in-flight state where
 * the button must not be pressable twice.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTarget } from '../../.storybook/touchTarget'
import { atViewport, expectLeftEdge, expectNoPageOverflow, expectSpans, expectTheme, expectViewport, type ViewportName } from '../../.storybook/viewports'
import { Login } from './Login'

const meta = {
  title: 'Shell/Login',
  component: Login,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  beforeEach: stubFetch(() => json({ email: 'alanna@aetheril.test', role: 'dm' })),
  decorators: [withShell({ authStatus: 'unauthenticated' })],
} satisfies Meta<typeof Login>

export default meta
type Story = StoryObj<typeof meta>

export const Empty: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('textbox', { name: 'Email' })).toHaveValue('')
    await expect(canvas.getByRole('button', { name: 'Sign in' })).toBeEnabled()
    await expect(canvas.queryByRole('alert')).not.toBeInTheDocument()
  },
}

/**
 * Signed in by keyboard alone: Tab to Email, Tab to Password, Tab to the
 * button, Enter. No pointer anywhere.
 */
export const SignedInByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    const email = canvas.getByRole('textbox', { name: 'Email' })
    await expect(email).toHaveFocus()
    await userEvent.keyboard('alanna@aetheril.test')

    await userEvent.tab()
    const password = canvas.getByLabelText('Password')
    await expect(password).toHaveFocus()
    await userEvent.keyboard('a-long-enough-password')

    await userEvent.tab()
    const submit = canvas.getByRole('button', { name: 'Sign in' })
    await expect(submit).toHaveFocus()
    await userEvent.keyboard('{Enter}')

    // The form accepted it: no error was raised.
    await expect(canvas.queryByRole('alert')).not.toBeInTheDocument()
  },
}

/**
 * An invalid submission: submitted empty, from the keyboard. The message is an
 * `alert`, so it is announced rather than only seen.
 */
export const SubmittedEmpty: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: 'Sign in' }).focus()
    await userEvent.keyboard('{Enter}')
    const alert = await canvas.findByRole('alert')
    await expect(alert).toHaveTextContent('Enter your email and password.')
  },
}

/** An email but no password — the same guard, from the other side. */
export const PasswordMissing: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const email = canvas.getByRole('textbox', { name: 'Email' })
    email.focus()
    await userEvent.keyboard('alanna@aetheril.test')
    canvas.getByRole('button', { name: 'Sign in' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toHaveTextContent('Enter your email and password.')
  },
}

/** The server refused the credentials. Its message is shown, not a generic one. */
export const RejectedByTheServer: Story = {
  beforeEach: stubFetch(() => json({ detail: 'Email or password is incorrect.' }, 401)),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('textbox', { name: 'Email' }).focus()
    await userEvent.keyboard('alanna@aetheril.test')
    canvas.getByLabelText('Password').focus()
    await userEvent.keyboard('wrong-password')
    canvas.getByRole('button', { name: 'Sign in' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toHaveTextContent('Email or password is incorrect.')
  },
}

/** The service is unreachable — a network failure, not a refusal. */
export const ServiceUnreachable: Story = {
  beforeEach: stubFetch(() => Promise.reject(new Error('network down'))),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('textbox', { name: 'Email' }).focus()
    await userEvent.keyboard('alanna@aetheril.test')
    canvas.getByLabelText('Password').focus()
    await userEvent.keyboard('a-long-enough-password')
    canvas.getByRole('button', { name: 'Sign in' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toHaveTextContent(/couldn't reach the service/i)
  },
}

/** In flight: the button says so and refuses a second press. */
export const Submitting: Story = {
  beforeEach: stubFetch(() => pending()),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('textbox', { name: 'Email' }).focus()
    await userEvent.keyboard('alanna@aetheril.test')
    canvas.getByLabelText('Password').focus()
    await userEvent.keyboard('a-long-enough-password')
    canvas.getByRole('button', { name: 'Sign in' }).focus()
    await userEvent.keyboard('{Enter}')

    const submit = await canvas.findByRole('button', { name: 'Signing in…' })
    await expect(submit).toBeDisabled()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkWithError: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: 'Sign in' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toBeVisible()
  },
}

// ── Phone (agent-forge-harness-0rn) ──────────────────────────────────────────
// A regression pin for the sign-in card on a phone. Login was already one
// column (a row flexbox with one centred child lays out as one), so nothing
// here is the phone block's mutation killer; Signup and the session-check
// screen are. What this pins is the reflow and the 44px floors at the widths
// every player signs in at (owner decision D-1).

async function expectPhoneSignIn(canvasElement: HTMLElement, viewport: ViewportName): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await expectNoPageOverflow()
  const card = canvasElement.querySelector('.auth-screen__card')
  if (!(card instanceof HTMLElement)) throw new Error('no sign-in card')
  await expectLeftEdge(card, 16)
  await expectTouchTarget(canvas, 'Sign in')
  await expectSpans(canvas.getByRole('button', { name: 'Sign in' }), card)
  const rows = Array.from(canvasElement.querySelectorAll('.aether-field__row'))
  await expect(rows).toHaveLength(2)
  for (const row of rows) {
    await expect(row.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  }
}

export const Phone390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => expectPhoneSignIn(canvasElement, 'phone390'),
}

export const Phone320: Story = {
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => expectPhoneSignIn(canvasElement, 'phone320'),
}

/** The error message is the longest line on the card; it wraps, not scrolls. */
export const DarkPhone320WithError: Story = {
  ...atViewport('phone320', 'dark'),
  beforeEach: stubFetch(() => json({ detail: 'Email or password is incorrect.' }, 401)),
  play: async ({ canvasElement }) => {
    await expectViewport('phone320')
    await expectTheme('dark')
    const canvas = within(canvasElement)
    canvas.getByRole('textbox', { name: 'Email' }).focus()
    await userEvent.keyboard('alanna@aetheril.test')
    canvas.getByLabelText('Password').focus()
    await userEvent.keyboard('wrong-password')
    canvas.getByRole('button', { name: 'Sign in' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toBeVisible()
    await expectPhoneSignIn(canvasElement, 'phone320')
  },
}
