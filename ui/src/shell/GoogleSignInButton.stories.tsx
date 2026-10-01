/**
 * GoogleSignInButton -- the three placements, in both themes (lvs7 pr-b).
 *
 * Sign-in is a link; sign-up and link are submit buttons inside a form. Each
 * story asserts the name Google's guidelines require, the 44px target, the
 * theme's published colours, a visible keyboard focus ring and that the logo is
 * hidden from assistive tech. The addon-a11y gate (axe, 'error') runs on every
 * one of them.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { tabTo } from '../../.storybook/keyboard'
import { GoogleSignInButton, type GoogleButtonKind } from './GoogleSignInButton'

const meta = {
  title: 'Shell/GoogleSignInButton',
  component: GoogleSignInButton,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  decorators: [
    (Story) => (
      <form method="post" action="/auth/google/start" style={{ maxWidth: 320 }} onSubmit={(e) => e.preventDefault()}>
        <Story />
      </form>
    ),
  ],
} satisfies Meta<typeof GoogleSignInButton>

export default meta
type Story = StoryObj<typeof meta>

const LABELS: Record<GoogleButtonKind, string> = {
  signin: 'Sign in with Google',
  signup: 'Sign up with Google',
  link: 'Continue with Google',
}

/** Google's published button colours for each app theme. */
const PUBLISHED = {
  light: { background: 'rgb(255, 255, 255)', border: 'rgb(116, 119, 117)', color: 'rgb(31, 31, 31)' },
  dark: { background: 'rgb(19, 19, 20)', border: 'rgb(142, 145, 143)', color: 'rgb(227, 227, 227)' },
} as const

async function expectGoogleButton(
  canvasElement: HTMLElement,
  kind: GoogleButtonKind,
  theme: 'light' | 'dark',
): Promise<void> {
  const canvas = within(canvasElement)
  const role = kind === 'signin' ? 'link' : 'button'
  const control = canvas.getByRole(role, { name: LABELS[kind] })

  // Name: the label alone; the logo is decorative.
  const logo = control.querySelector('svg')
  await expect(logo).toHaveAttribute('aria-hidden', 'true')
  await expect(control).toHaveAccessibleName(LABELS[kind])

  // 44px target, both axes.
  const box = control.getBoundingClientRect()
  await expect(box.height).toBeGreaterThanOrEqual(44)
  await expect(box.width).toBeGreaterThanOrEqual(44)

  // The theme's published Google colours.
  const style = getComputedStyle(control)
  await expect(style.backgroundColor).toBe(PUBLISHED[theme].background)
  await expect(style.borderTopColor).toBe(PUBLISHED[theme].border)
  await expect(style.color).toBe(PUBLISHED[theme].color)
  await expect(style.fontWeight).toBe('500')
  await expect(style.fontSize).toBe('14px')

  // Reached and operable from the keyboard, with a ring that is actually drawn.
  await tabTo(control)
  const focused = getComputedStyle(control)
  await expect(focused.outlineStyle).not.toBe('none')
  await expect(parseFloat(focused.outlineWidth)).toBeGreaterThanOrEqual(2)
}

export const SignIn: Story = {
  args: { kind: 'signin' },
  play: async ({ canvasElement }) => expectGoogleButton(canvasElement, 'signin', 'light'),
}

export const SignInDark: Story = {
  args: { kind: 'signin' },
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => expectGoogleButton(canvasElement, 'signin', 'dark'),
}

export const SignUp: Story = {
  args: { kind: 'signup' },
  play: async ({ canvasElement }) => expectGoogleButton(canvasElement, 'signup', 'light'),
}

export const SignUpDark: Story = {
  args: { kind: 'signup' },
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => expectGoogleButton(canvasElement, 'signup', 'dark'),
}

export const Link: Story = {
  args: { kind: 'link' },
  play: async ({ canvasElement }) => expectGoogleButton(canvasElement, 'link', 'light'),
}

export const LinkDark: Story = {
  args: { kind: 'link' },
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => expectGoogleButton(canvasElement, 'link', 'dark'),
}
