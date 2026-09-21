/**
 * The surfaces a person sees before they have a session: the sign-in screen,
 * the invite deep-link, and what a REFUSED sign-in says.
 */

import { expect, test } from './fixtures'

test('the root of a signed-out browser is the sign-in screen, and the workspace is not reachable from it', async ({
  page,
}) => {
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Aetheril' })).toBeVisible()
  await expect(page.getByText('Sign in to continue')).toBeVisible()
  await expect(page.getByLabel('Email')).toBeVisible()
  await expect(page.getByLabel('Password')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()

  // No session, no workspace — not the channel band, not the way in.
  await expect(page.getByRole('navigation', { name: 'Channels' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toHaveCount(0)

  // An empty submit is refused in the browser, without a round trip.
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toHaveText('Enter your email and password.')
})

test('an invite deep-link offers account creation, spends the token from the address bar, and can hand back to sign-in', async ({
  page,
}) => {
  // Deliberately not a seeded token: this test is about the SCREEN an invite
  // link opens and what it does with the token, not about redeeming one.
  await page.goto('/#invite=not-a-real-invite')

  await expect(
    page.getByText("You've been invited — create your account"),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: 'Create account' })).toBeVisible()

  // The invite is a single-use credential: it must not be left in the address
  // bar (and from there in browser history) once the app has taken it.
  await expect(page).not.toHaveURL(/invite=/)

  // A token the service does not know is refused, and says only that.
  await page.getByLabel('Email').fill('uninvited@example.com')
  await page.getByLabel('Password').fill('e2e-password-123')
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('alert')).toHaveText('Unknown invite link.')

  await page.getByRole('button', { name: 'Already have an account? Sign in' }).click()
  await expect(page.getByText('Sign in to continue')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Create account' })).toHaveCount(0)
})

test('a refused sign-in shows an error, and says exactly the same thing whether or not the account exists', async ({
  page,
  accounts,
}) => {
  const [registered] = accounts
  const wrongPassword = 'not-the-right-password'
  const alert = page.getByRole('alert')

  await page.goto('/')

  // 1. An account that DOES exist, with the wrong password.
  const refusalForRegistered = page.waitForResponse(
    (response) =>
      response.url().endsWith('/auth/login') &&
      response.request().method() === 'POST',
  )
  await page.getByLabel('Email').fill(registered.email)
  await page.getByLabel('Password').fill(wrongPassword)
  await page.getByRole('button', { name: 'Sign in' }).click()
  const registeredResponse = await refusalForRegistered
  await expect(alert).toBeVisible()
  const shownForRegistered = await alert.innerText()
  expect(shownForRegistered.trim()).not.toBe('')

  // 2. An address that has never been registered.
  //
  // Waiting on the RESPONSE rather than on the message changing is what makes
  // this a real comparison: the two messages are supposed to be identical, so
  // an assertion on the text alone would be satisfied by the first attempt's
  // error still being on screen and would pass even if the second request
  // never happened.
  const refusalForUnknown = page.waitForResponse(
    (response) =>
      response.url().endsWith('/auth/login') &&
      response.request().method() === 'POST',
  )
  await page.getByLabel('Email').fill('no-such-account@example.com')
  await page.getByLabel('Password').fill(wrongPassword)
  await page.getByRole('button', { name: 'Sign in' }).click()
  const unknownResponse = await refusalForUnknown
  await expect(alert).toBeVisible()

  // The whole point: nothing here distinguishes "wrong password" from "no such
  // account" — not the words on screen, not the status, not the wire body.
  await expect(alert).toHaveText(shownForRegistered)
  expect(unknownResponse.status()).toBe(registeredResponse.status())
  expect(unknownResponse.status()).toBe(401)
  expect(await unknownResponse.json()).toEqual(await registeredResponse.json())

  // Still signed out, both times.
  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toHaveCount(0)
})
