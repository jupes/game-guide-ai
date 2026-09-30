/**
 * The session itself: signing in, surviving a reload, and what signing out
 * actually leaves behind for the next person on this browser.
 */

import { expect, signIn, test } from './fixtures'

test('a signed-in conversation survives a reload, and signing out leaves nothing for the next account', async ({
  page,
  accounts,
}) => {
  const [first, second] = accounts
  const prompt = 'Who can take the Dodge action?'

  await page.goto('/')
  await signIn(page, first)

  await page.getByRole('button', { name: 'Enter the Tavern' }).click()
  const channels = page.getByRole('navigation', { name: 'Channels' })
  await channels.getByRole('button', { name: 'Rules' }).click()
  await page.getByRole('button', { name: 'New conversation' }).click()
  await page.getByPlaceholder('Ask…').fill(prompt)
  await page.getByRole('button', { name: 'Send message' }).click()
  await expect(page.getByText(`E2E rules answer: ${prompt}`)).toBeVisible()

  // ── The session and the conversation both survive a reload ────────────────
  await page.reload()
  // Straight back into the app: no sign-in screen, because the session cookie
  // is still good. The landing screen's channel chips are the way back in.
  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toBeVisible()
  await page.getByRole('button', { name: 'Rules' }).click()
  await page.getByRole('button', { name: prompt, exact: true }).click()
  await expect(page.getByText(`E2E rules answer: ${prompt}`)).toBeVisible()

  // ── Signing out ───────────────────────────────────────────────────────────
  // agent-forge-harness-3j4: the popover is a labelled group of buttons, not
  // an ARIA menu — scoped + exact, since Playwright's `name` is otherwise a
  // case-insensitive substring match and the LeftNav lists conversations as
  // buttons titled by the user's own prompts.
  await page.getByRole('button', { name: 'Open user menu' }).click()
  await page
    .getByRole('group', { name: 'User menu', exact: true })
    .getByRole('button', { name: 'Sign out', exact: true })
    .click()
  await expect(page.getByText('Sign in to continue')).toBeVisible()

  // The SERVER ended it, not just the tab: the cookie is httpOnly, so a reload
  // is the only way to tell a real sign-out from a cosmetic one.
  await page.reload()
  await expect(page.getByText('Sign in to continue')).toBeVisible()

  // ── The next account starts clean ─────────────────────────────────────────
  await signIn(page, second)
  await page.getByRole('button', { name: 'Rules' }).click()
  // Anchor on the workspace being rendered before asserting an absence. The
  // conversation list is read synchronously from localStorage as the nav
  // renders — there is no later fetch that could add a row behind the
  // assertion — so once the channel band is up, the list is final.
  await expect(channels).toBeVisible()
  await expect(page.getByRole('button', { name: prompt, exact: true })).toHaveCount(0)
  await expect(page.getByText(`E2E rules answer: ${prompt}`)).toHaveCount(0)
})
