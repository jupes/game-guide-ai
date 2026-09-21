/**
 * What the app does when the API does not answer.
 *
 * The guards in fixtures.ts already fail these tests on an uncaught exception
 * or an unhandled promise rejection, which is half of what "degrades" means
 * here; the assertions below are the other half — that something useful is on
 * screen and the app can still be used afterwards.
 */

import { expect, signIn, test } from './fixtures'

test('a session check that cannot be completed offers a retry instead of a blank screen or a false sign-out', async ({
  page,
}) => {
  await page.route('**/auth/me', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'message history unavailable' }),
    }),
  )

  await page.goto('/')

  await expect(
    page.getByRole('heading', { name: 'Can’t reach the service' }),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible()
  // A 503 is not a logout. Showing the sign-in form here would ask for
  // credentials that the same unreachable backend has to check.
  await expect(page.getByText('Sign in to continue')).toHaveCount(0)
  await expect(page.getByLabel('Password')).toHaveCount(0)

  // The outage passes: a click, not a page reload and not a re-login.
  await page.unroute('**/auth/me')
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByText('Sign in to continue')).toBeVisible()
})

test('a failing chat request is reported in the thread, and the next question still goes through', async ({
  page,
  accounts,
}) => {
  await page.goto('/')
  await signIn(page, accounts[0])
  await page.getByRole('button', { name: 'Sage' }).click()
  await page.getByRole('button', { name: 'New conversation' }).click()

  await page.route('**/chat', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'upstream unavailable' }),
    }),
  )

  const failed = 'Does this fail politely?'
  await page.getByPlaceholder('Ask…').fill(failed)
  await page.getByRole('button', { name: 'Send message' }).click()

  await expect(
    page.getByText(
      'Service unavailable (starting up or upstream error) — try again shortly.',
    ),
  ).toBeVisible()
  // The thread is intact: the question that failed is still readable, so the
  // reader can see WHICH one failed.
  await expect(
    page.getByRole('main').getByText(failed, { exact: true }),
  ).toBeVisible()

  // ...and the composer recovered rather than being wedged by the failure.
  await page.unroute('**/chat')
  const recovered = 'And once the tavern reopens?'
  await page.getByPlaceholder('Ask…').fill(recovered)
  await page.getByRole('button', { name: 'Send message' }).click()
  await expect(page.getByText(`E2E sage answer: ${recovered}`)).toBeVisible()
})
