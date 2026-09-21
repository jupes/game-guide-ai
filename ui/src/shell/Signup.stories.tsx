/**
 * Signup — redeem a one-time invite.
 *
 * Unlike Login it validates client-side first (`validateCredentials`), so there
 * are three distinct invalid submissions before any request is made, plus the
 * server's own refusals — including a spent invite, which is the one a tester
 * will actually hit.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { Signup } from './Signup'

const meta = {
  title: 'Shell/Signup',
  component: Signup,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  beforeEach: stubFetch(() => json({ email: 'newcomer@aetheril.test', role: 'player' })),
  args: { invite: 'invite-token-from-the-fragment', onUseLogin: fn() },
  decorators: [withShell({ authStatus: 'unauthenticated' })],
} satisfies Meta<typeof Signup>

export default meta
type Story = StoryObj<typeof meta>

export const Empty: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText("You've been invited — create your account")).toBeInTheDocument()
    // The password rule is stated up front, not only after a failure.
    await expect(canvas.getByText('At least 8 characters')).toBeInTheDocument()
  },
}

/** Created by keyboard alone. */
export const CreatedByKeyboard: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    await expect(canvas.getByRole('textbox', { name: 'Email' })).toHaveFocus()
    await userEvent.keyboard('newcomer@aetheril.test')

    await userEvent.tab()
    await expect(canvas.getByLabelText('Password')).toHaveFocus()
    await userEvent.keyboard('correct-horse-battery')

    await userEvent.tab()
    const submit = canvas.getByRole('button', { name: 'Create account' })
    await expect(submit).toHaveFocus()
    await userEvent.keyboard('{Enter}')

    // The invite is spent — it must never be offered again.
    await expect(args.onUseLogin).toHaveBeenCalled()
  },
}

/** Nothing filled in. */
export const SubmittedEmpty: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: 'Create account' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toHaveTextContent('Enter your email address.')
  },
}

/**
 * An address with no `@`.
 *
 * Worth recording precisely, because it is not what the code reads like: the
 * field is `type="email"`, so the BROWSER's constraint validation refuses the
 * submit and `handleSubmit` never runs. `validateCredentials`' own "Enter a
 * valid email address." is a second line of defence that this UI cannot reach —
 * it still earns its keep for a non-browser client, and its unit test covers
 * it, but no story can show it here without lying about the widget.
 */
export const InvalidEmailIsRefusedByTheBrowser: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const email = canvas.getByRole('textbox', { name: 'Email' })
    email.focus()
    await userEvent.keyboard('newcomer-at-aetheril')
    await expect(email).toHaveAttribute('type', 'email')
    await expect((email as HTMLInputElement).checkValidity()).toBe(false)

    canvas.getByRole('button', { name: 'Create account' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(canvas.queryByRole('alert')).not.toBeInTheDocument()
  },
}

/** Seven characters. The rule is stated in the message, not just enforced. */
export const PasswordTooShort: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('textbox', { name: 'Email' }).focus()
    await userEvent.keyboard('newcomer@aetheril.test')
    canvas.getByLabelText('Password').focus()
    await userEvent.keyboard('sevench')
    canvas.getByRole('button', { name: 'Create account' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toHaveTextContent(
      'Password must be at least 8 characters.',
    )
  },
}

/** The invite has already been redeemed — the state a re-used link produces. */
export const InviteAlreadySpent: Story = {
  beforeEach: stubFetch(() => json({ detail: 'That invite has already been used.' }, 400)),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('textbox', { name: 'Email' }).focus()
    await userEvent.keyboard('newcomer@aetheril.test')
    canvas.getByLabelText('Password').focus()
    await userEvent.keyboard('correct-horse-battery')
    canvas.getByRole('button', { name: 'Create account' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toHaveTextContent(
      'That invite has already been used.',
    )
  },
}

/** In flight. */
export const Submitting: Story = {
  beforeEach: stubFetch(() => pending()),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('textbox', { name: 'Email' }).focus()
    await userEvent.keyboard('newcomer@aetheril.test')
    canvas.getByLabelText('Password').focus()
    await userEvent.keyboard('correct-horse-battery')
    canvas.getByRole('button', { name: 'Create account' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('button', { name: 'Creating account…' })).toBeDisabled()
  },
}

/** The way out for someone who already has an account. */
export const SwitchToSignIn: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const link = canvas.getByRole('button', { name: /already have an account/i })
    link.focus()
    await userEvent.keyboard('{Enter}')
    await expect(args.onUseLogin).toHaveBeenCalled()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkWithError: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: 'Create account' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(await canvas.findByRole('alert')).toBeVisible()
  },
}
