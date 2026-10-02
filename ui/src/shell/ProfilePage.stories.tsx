/**
 * ProfilePage — display name and avatar tone, both locally stubbed, plus the
 * read-only role and the list of fields that do not exist yet.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { tabTo } from '../../.storybook/keyboard'
import { json, stubFetch, withShell } from '../../.storybook/shellHarness'
import { atViewport, expectLeftEdge, expectNoPageOverflow, expectSpans, expectViewport, type ViewportName } from '../../.storybook/viewports'
import { ProfilePage } from './ProfilePage'
import { clearGoogleOutcome, setGoogleOutcome } from './googleOutcome'

const meta = {
  title: 'Shell/ProfilePage',
  component: ProfilePage,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  decorators: [withShell({ screen: 'profile' })],
} satisfies Meta<typeof ProfilePage>

export default meta
type Story = StoryObj<typeof meta>

export const DungeonMaster: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('textbox', { name: 'Display name' })).toHaveValue('Alanna Quill')
    const roleSwitch = canvas.getByRole('switch', { name: 'Dungeon Master role' })
    await expect(roleSwitch).toBeChecked()
    await expect(roleSwitch).toBeDisabled()
  },
}

export const Player: Story = {
  decorators: [withShell({ screen: 'profile', role: 'player', displayName: 'Tam Underbough' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('switch', { name: 'Dungeon Master role' })).not.toBeChecked()
  },
}

/**
 * Edited by keyboard alone: Tab into the field, type, and the avatar's initials
 * follow. The tone buttons are `aria-pressed` toggles, so a screen reader
 * hears which colour is selected.
 */
export const RenamedByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = canvas.getByRole('textbox', { name: 'Display name' })
    await userEvent.tab()
    await expect(field).toHaveFocus()

    await userEvent.keyboard('{Control>}a{/Control}Grivvel Oakenshadow')
    await expect(field).toHaveValue('Grivvel Oakenshadow')
    // The header avatar and the four tone swatches all take their initials
    // from the same value, so the rename is visible immediately.
    await expect(canvas.getAllByText('GO')).toHaveLength(5)
  },
}

/**
 * Tone selection, driven from the keyboard, with the pressed state asserted.
 *
 * The swatch is reached with Tab presses (`tabTo`) rather than `.focus()`, so
 * "from the keyboard" covers getting there as well as activating it — a colour
 * swatch is exactly the kind of control that ends up as a pointer-only
 * `<div onClick>`.
 */
export const TonePickedByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const ember = canvas.getByRole('button', { name: 'Ember avatar' })
    await expect(canvas.getByRole('button', { name: 'Gold avatar' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await tabTo(ember)
    await userEvent.keyboard('{Enter}')
    await expect(ember).toHaveAttribute('aria-pressed', 'true')
    await expect(canvas.getByRole('button', { name: 'Gold avatar' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  },
}

/** An empty display name — the field allows it, and nothing crashes on initials. */
export const EmptyDisplayName: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = canvas.getByRole('textbox', { name: 'Display name' })
    field.focus()
    await userEvent.keyboard('{Control>}a{/Control}{Backspace}')
    await expect(field).toHaveValue('')
  },
}

/** A name long enough to wrap the header and stress the card's measure. */
export const LongDisplayName: Story = {
  decorators: [
    withShell({
      screen: 'profile',
      displayName: 'Archmagister Seraphina Duskwhisper of the Ninefold Spire',
      avatarTone: 'arcane',
    }),
  ],
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkVerdigris: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell({ screen: 'profile', avatarTone: 'verdigris' })],
}

/**
 * agent-forge-harness-0rn: Profile on a phone. The page's side padding drops
 * to the gutter, "Back to chat" spans the card, and each avatar-tone
 * swatch keeps its 44px target. `gutterEdge` is false where the card has
 * reached its own max-width and is centred instead.
 */
async function expectPhoneProfile(
  canvasElement: HTMLElement,
  viewport: ViewportName,
  gutterEdge = true,
): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await expectNoPageOverflow()
  const card = canvasElement.querySelector('.profile-page__card')
  if (!(card instanceof HTMLElement)) throw new Error('no profile card')
  if (gutterEdge) await expectLeftEdge(card, 16)
  await expectSpans(canvas.getByRole('button', { name: 'Back to chat' }), card)
  const swatches = Array.from(canvasElement.querySelectorAll('.profile-page__tone'))
  await expect(swatches.length).toBeGreaterThan(0)
  for (const swatch of swatches) {
    const box = swatch.getBoundingClientRect()
    await expect(box.width).toBeGreaterThanOrEqual(44)
    await expect(box.height).toBeGreaterThanOrEqual(44)
  }
}

export const Phone390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => expectPhoneProfile(canvasElement, 'phone390'),
}

/** AC-1's narrowest phone. */
export const Phone320: Story = {
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => expectPhoneProfile(canvasElement, 'phone320'),
}

/** One pixel inside the phone rule: "Back to chat" still spans the card. */
export const Edge599: Story = {
  ...atViewport('edge599'),
  play: async ({ canvasElement }) => expectPhoneProfile(canvasElement, 'edge599', false),
}

// ── Sign in with Google (lvs7 pr-b) ──────────────────────────────────────────
// The section is drawn only when the service offers Google AND has said what
// this account's link status is. Each story answers those two questions.

function googleStub(link: { linked: boolean; email: string | null; has_password: boolean } | null) {
  return stubFetch((url) => {
    if (url.endsWith('/auth/google/available')) return json({ available: true })
    if (url.endsWith('/auth/google/link')) {
      return link === null ? json({ detail: 'authentication required' }, 401) : json(link)
    }
    return json({ detail: 'Not Found' }, 404)
  })
}

/** Feature off (every Google route is a 404): no section, and no mention. */
export const GoogleOff: Story = {
  beforeEach: stubFetch(() => json({ detail: 'Not Found' }, 404)),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('heading', { name: 'Profile' })).toBeVisible()
    await expect(canvas.queryByText(/google/i)).not.toBeInTheDocument()
  },
}

/** Not linked: the form asks for the current password, then continues to Google. */
export const GoogleLinkForm: Story = {
  beforeEach: googleStub({ linked: false, email: null, has_password: true }),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const section = await canvas.findByRole('region', { name: 'Google account' })
    const inSection = within(section)
    const password = inSection.getByLabelText('Current password')
    await expect(password).toHaveAttribute('type', 'password')
    await expect(password).toHaveAttribute('autocomplete', 'current-password')
    await expect(password).toBeRequired()
    await expect(password.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    const submit = inSection.getByRole('button', { name: 'Continue with Google' })
    await expect(submit.closest('form')).toHaveAttribute('action', '/auth/google/start')
    await expect(submit.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  },
}

/** Reached and filled from the keyboard: Tab to the field, type, Tab to the button. */
export const GoogleLinkFormByKeyboard: Story = {
  beforeEach: googleStub({ linked: false, email: null, has_password: true }),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const section = await canvas.findByRole('region', { name: 'Google account' })
    const password = within(section).getByLabelText('Current password')
    const submit = within(section).getByRole('button', { name: 'Continue with Google' })
    await tabTo(password)
    await userEvent.keyboard('correct-horse-battery')
    await expect(password).toHaveValue('correct-horse-battery')
    await tabTo(submit)
    // The ring is drawn on the focused control.
    await expect(getComputedStyle(submit).outlineStyle).not.toBe('none')
  },
}

export const GoogleLinkFormDark: Story = {
  globals: { theme: 'dark' },
  beforeEach: googleStub({ linked: false, email: null, has_password: true }),
  play: async ({ canvasElement }) => {
    const submit = await within(canvasElement).findByRole('button', { name: 'Continue with Google' })
    await expect(getComputedStyle(submit).backgroundColor).toBe('rgb(19, 19, 20)')
  },
}

/** Linked: says which address, and offers no way to remove it. */
export const GoogleLinked: Story = {
  beforeEach: googleStub({ linked: true, email: 'alanna@gmail.example', has_password: true }),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const section = await canvas.findByRole('region', { name: 'Google account' })
    await expect(section).toHaveTextContent('Linked to alanna@gmail.example')
    await expect(within(section).queryByRole('button')).not.toBeInTheDocument()
  },
}

export const GoogleLinkedDark: Story = {
  globals: { theme: 'dark' },
  beforeEach: googleStub({ linked: true, email: 'alanna@gmail.example', has_password: true }),
  play: async ({ canvasElement }) => {
    await expect(await within(canvasElement).findByRole('region', { name: 'Google account' })).toBeVisible()
  },
}

/** An account made through Google has no password to type. */
export const GoogleOnlyAccount: Story = {
  beforeEach: googleStub({ linked: true, email: 'alanna@gmail.example', has_password: false }),
  play: async ({ canvasElement }) => {
    const section = await within(canvasElement).findByRole('region', { name: 'Google account' })
    await expect(section).toHaveTextContent('You sign in with Google.')
    await expect(within(section).queryByLabelText(/password/i)).not.toBeInTheDocument()
  },
}

/** The link just succeeded: a polite status, announced rather than only seen. */
export const GoogleJustLinked: Story = {
  beforeEach: () => {
    const restore = googleStub({ linked: true, email: 'alanna@gmail.example', has_password: true })()
    setGoogleOutcome({ search: '?google=linked', pathname: '/profile' })
    return () => {
      clearGoogleOutcome()
      restore()
    }
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('status')).toHaveTextContent('Google account linked.')
    await expect(canvas.queryByRole('alert')).not.toBeInTheDocument()
  },
}

/** The password was wrong: an alert, and no Google redirect happened. */
export const GoogleLinkRefusedDark: Story = {
  globals: { theme: 'dark' },
  beforeEach: () => {
    const restore = googleStub({ linked: false, email: null, has_password: true })()
    setGoogleOutcome({ search: '?google=reauth_failed', pathname: '/profile' })
    return () => {
      clearGoogleOutcome()
      restore()
    }
  },
  play: async ({ canvasElement }) => {
    await expect(await within(canvasElement).findByRole('alert')).toHaveTextContent(
      "That password isn't right, so Google wasn't linked.",
    )
  },
}

export const GoogleLinkFormPhone320: Story = {
  ...atViewport('phone320'),
  beforeEach: googleStub({ linked: false, email: null, has_password: true }),
  play: async ({ canvasElement }) => {
    await expectViewport('phone320')
    const section = await within(canvasElement).findByRole('region', { name: 'Google account' })
    const card = canvasElement.querySelector('.profile-page__card')
    if (!(card instanceof HTMLElement)) throw new Error('no profile card')
    await expectNoPageOverflow()
    await expectSpans(within(section).getByRole('button', { name: 'Continue with Google' }), section)
    await expectSpans(section, card)
  },
}
