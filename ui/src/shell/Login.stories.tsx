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
import { clearGoogleOutcome, setGoogleOutcome } from './googleOutcome'

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

async function expectPhoneSignInCore(canvasElement: HTMLElement, viewport: ViewportName): Promise<HTMLElement> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await expectNoPageOverflow()
  const card = canvasElement.querySelector('.auth-screen__card')
  if (!(card instanceof HTMLElement)) throw new Error('no sign-in card')
  await expectTouchTarget(canvas, 'Sign in')
  await expectSpans(canvas.getByRole('button', { name: 'Sign in' }), card)
  const rows = Array.from(canvasElement.querySelectorAll('.aether-field__row'))
  await expect(rows).toHaveLength(2)
  for (const row of rows) {
    await expect(row.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  }
  return card
}

/** Below the card's 360px max-width (<= 392px total: 360 + the 16px gutter on
 * each side), the card fills the row and sits flush at the gutter. */
async function expectPhoneSignIn(canvasElement: HTMLElement, viewport: ViewportName): Promise<void> {
  const card = await expectPhoneSignInCore(canvasElement, viewport)
  await expectLeftEdge(card, 16)
}

export const Phone390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => expectPhoneSignIn(canvasElement, 'phone390'),
}

export const Phone320: Story = {
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => expectPhoneSignIn(canvasElement, 'phone320'),
}

/** agent-forge-harness-zh9 L-1: AC-1's own width list (320/375/390/599) had
 * no Login story at 375 or 599. */
export const Phone375: Story = {
  ...atViewport('phone375'),
  play: async ({ canvasElement }) => expectPhoneSignIn(canvasElement, 'phone375'),
}

/** Above 392px the 360px max-width binds: the card is centred, not flush to
 * the gutter, so this pins equal margins instead of `expectLeftEdge`. */
export const Edge599: Story = {
  ...atViewport('edge599'),
  play: async ({ canvasElement }) => {
    const card = await expectPhoneSignInCore(canvasElement, 'edge599')
    const box = card.getBoundingClientRect()
    await expect(Math.abs(box.left - (window.innerWidth - box.right))).toBeLessThanOrEqual(1)
  },
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

// ── Sign in with Google (lvs7 pr-b) ──────────────────────────────────────────
// The control exists only when the service says `{"available": true}`. These
// stories answer that one question and nothing else; every other request gets
// the sign-in refusal the stories above use.

function googleAvailableStub(): () => () => void {
  return stubFetch((url) =>
    url.endsWith('/auth/google/available')
      ? json({ available: true })
      : json({ detail: 'Email or password is incorrect.' }, 401),
  )
}

/** Feature off: nothing about Google is drawn, and nothing is left disabled. */
export const GoogleOff: Story = {
  beforeEach: stubFetch(() => json({ detail: 'Not Found' }, 404)),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Sign in' })).toBeEnabled()
    await expect(canvas.queryByText(/google/i)).not.toBeInTheDocument()
  },
}

/** Feature on: a link above the email form, "or" between, the form untouched. */
export const GoogleOn: Story = {
  beforeEach: googleAvailableStub(),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const link = await canvas.findByRole('link', { name: 'Sign in with Google' })
    await expect(link).toHaveAttribute('href', '/auth/google/start')
    await expect(link.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    await expect(canvas.getByText('or')).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Sign in' })).toBeEnabled()
    await expect(canvas.getByRole('textbox', { name: 'Email' })).toBeVisible()
  },
}

export const GoogleOnDark: Story = {
  globals: { theme: 'dark' },
  beforeEach: googleAvailableStub(),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const link = await canvas.findByRole('link', { name: 'Sign in with Google' })
    await expectTheme('dark')
    await expect(getComputedStyle(link).backgroundColor).toBe('rgb(19, 19, 20)')
  },
}

/** What the callback sends someone back with when no account matched. */
export const GoogleNoAccount: Story = {
  beforeEach: () => {
    const restore = googleAvailableStub()()
    setGoogleOutcome({ search: '?google=no_account', pathname: '/' })
    return () => {
      clearGoogleOutcome()
      restore()
    }
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('alert')).toHaveTextContent(
      "We couldn't find an account for that Google sign-in.",
    )
    await expect(await canvas.findByRole('link', { name: 'Sign in with Google' })).toBeVisible()
  },
}

export const GoogleNoAccountDark: Story = {
  globals: { theme: 'dark' },
  beforeEach: () => {
    const restore = googleAvailableStub()()
    setGoogleOutcome({ search: '?google=no_account', pathname: '/' })
    return () => {
      clearGoogleOutcome()
      restore()
    }
  },
  play: async ({ canvasElement }) => {
    await expect(await within(canvasElement).findByRole('alert')).toBeVisible()
  },
}

export const GoogleOnPhone320: Story = {
  ...atViewport('phone320'),
  beforeEach: googleAvailableStub(),
  play: async ({ canvasElement }) => {
    await expectViewport('phone320')
    const canvas = within(canvasElement)
    const link = await canvas.findByRole('link', { name: 'Sign in with Google' })
    const card = canvasElement.querySelector('.auth-screen__card')
    if (!(card instanceof HTMLElement)) throw new Error('no sign-in card')
    await expectNoPageOverflow()
    await expectSpans(link, card)
  },
}
