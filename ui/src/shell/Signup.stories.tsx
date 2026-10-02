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
import { expectTouchTarget } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectSpans, expectViewport, type ViewportName } from '../../.storybook/viewports'
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

/**
 * agent-forge-harness-0rn: on a phone, "Already have an account? Sign in" is a
 * card action, so it spans the card instead of sitting as a centred link.
 */
async function expectPhoneSignup(canvasElement: HTMLElement, viewport: ViewportName): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await expectNoPageOverflow()
  const card = canvasElement.querySelector('.auth-screen__card')
  if (!(card instanceof HTMLElement)) throw new Error('no signup card')
  const toSignIn = canvas.getByRole('button', { name: 'Already have an account? Sign in' })
  await expectSpans(toSignIn, card)
  await expectTouchTarget(canvas, 'Already have an account? Sign in')
}

export const Phone390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => expectPhoneSignup(canvasElement, 'phone390'),
}

/** AC-1's narrowest phone. A regression pin, not the phone rule's killer:
 * at 320px the link's wrapped text already fills the card, so it spans with
 * or without the rule. Phone390 and Edge599 are the widths that catch a
 * narrowed or dropped rule. */
export const Phone320: Story = {
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => expectPhoneSignup(canvasElement, 'phone320'),
}

/** One pixel inside the phone rule: the link still spans the card. */
export const Edge599: Story = {
  ...atViewport('edge599'),
  play: async ({ canvasElement }) => expectPhoneSignup(canvasElement, 'edge599'),
}

// ── Sign in with Google (lvs7 pr-b) ──────────────────────────────────────────

function googleAvailableStub(): () => () => void {
  return stubFetch((url) =>
    url.endsWith('/auth/google/available')
      ? json({ available: true })
      : json({ detail: 'Unknown invite link.' }, 400),
  )
}

/** Feature off: the password form is the only way in, and nothing mentions Google. */
export const GoogleOff: Story = {
  beforeEach: stubFetch(() => json({ detail: 'Not Found' }, 404)),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Create account' })).toBeEnabled()
    await expect(canvas.queryByText(/google/i)).not.toBeInTheDocument()
  },
}

/**
 * Feature on. The invite rides in the POST body of a native form -- never in
 * an href or the address bar -- and the email form is still there.
 */
export const GoogleOn: Story = {
  beforeEach: googleAvailableStub(),
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const button = await canvas.findByRole('button', { name: 'Sign up with Google' })
    const form = button.closest('form')
    if (form === null) throw new Error('the Google button is not in a form')
    await expect(form).toHaveAttribute('method', 'post')
    await expect(form).toHaveAttribute('action', '/auth/google/start')
    await expect(form.querySelector('input[name="intent"]')).toHaveValue('invite')
    await expect(form.querySelector('input[name="invite"]')).toHaveValue(args.invite)
    await expect(canvasElement.querySelector('[href]')).toBeNull()
    await expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    await expect(canvas.getByRole('button', { name: 'Create account' })).toBeEnabled()
  },
}

export const GoogleOnDark: Story = {
  globals: { theme: 'dark' },
  beforeEach: googleAvailableStub(),
  play: async ({ canvasElement }) => {
    const button = await within(canvasElement).findByRole('button', { name: 'Sign up with Google' })
    await expect(getComputedStyle(button).backgroundColor).toBe('rgb(19, 19, 20)')
  },
}

export const GoogleOnPhone320: Story = {
  ...atViewport('phone320'),
  beforeEach: googleAvailableStub(),
  play: async ({ canvasElement }) => {
    await expectViewport('phone320')
    const canvas = within(canvasElement)
    const button = await canvas.findByRole('button', { name: 'Sign up with Google' })
    const card = canvasElement.querySelector('.auth-screen__card')
    if (!(card instanceof HTMLElement)) throw new Error('no signup card')
    await expectNoPageOverflow()
    await expectSpans(button, card)
  },
}
