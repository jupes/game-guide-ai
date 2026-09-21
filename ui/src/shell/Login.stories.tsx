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
