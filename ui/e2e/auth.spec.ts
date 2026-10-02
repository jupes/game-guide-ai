/**
 * The surfaces a person sees before they have a session: the sign-in screen,
 * the invite deep-link, and what a REFUSED sign-in says.
 */

import { expect, signIn, test } from './fixtures'

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

  // An empty submit is refused in the browser, WITHOUT a round trip. The
  // message alone does not show that: a server that answered 400 with the same
  // words would satisfy it. So count the requests too — an empty submit that
  // reached /auth/login would spend one of the caller's rate-limit attempts
  // (service/ratelimit.py spends before it validates) and hand an unauthenticated
  // caller a way to drain the budget with empty posts.
  const loginRequests: string[] = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/auth/login') {
      loginRequests.push(request.method())
    }
  })

  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toHaveText('Enter your email and password.')
  expect(loginRequests).toEqual([])
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

// ── Sign in with Google (lvs7 pr-b) ──────────────────────────────────────────
// This stack has Google OFF (no GOOGLE_OAUTH_CLIENT_ID). The property worth
// holding is that "off" is invisible: no button, no disabled stub, no mention
// anywhere a person signs in, signs up or edits their profile, and every Google
// route is an ordinary 404 through nginx. A Google-ON end to end needs a fake
// identity provider in service/e2e_app.py and is a separate bead.

const isAvailabilityCheck = (response: { url(): string }): boolean =>
  response.url().endsWith('/auth/google/available')

test('with Google off, the sign-in screen draws no Google control', async ({ page }) => {
  // Registered BEFORE the navigation: the question is asked as the page mounts,
  // and an absence asserted before it is answered would pass for the wrong reason.
  const answered = page.waitForResponse(isAvailabilityCheck)
  await page.goto('/')
  expect((await answered).status()).toBe(404)
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
  await expect(page.getByText(/google/i)).toHaveCount(0)
  await expect(page.getByRole('link', { name: /with Google/i })).toHaveCount(0)
})

test('with Google off, the invite screen draws no Google control', async ({ page }) => {
  const answered = page.waitForResponse(isAvailabilityCheck)
  await page.goto('/#invite=not-a-real-invite')
  expect((await answered).status()).toBe(404)
  await expect(page.getByRole('button', { name: 'Create account' })).toBeVisible()
  await expect(page.getByText(/google/i)).toHaveCount(0)
  await expect(page.getByRole('button', { name: /with Google/i })).toHaveCount(0)
})

test('with Google off, the profile page has no Google section', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[0])
  const answered = page.waitForResponse(isAvailabilityCheck)
  await page.goto('/profile')
  expect((await answered).status()).toBe(404)
  await expect(page.getByRole('heading', { name: 'Profile' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Google account' })).toHaveCount(0)
  await expect(page.getByText(/google/i)).toHaveCount(0)
})

test('with Google off, every Google route is a plain 404 through nginx, never a redirect', async ({
  request,
}) => {
  const unknown = await request.get('/auth/does-not-exist', { maxRedirects: 0 })
  expect(unknown.status()).toBe(404)
  for (const path of [
    '/auth/google/available',
    '/auth/google/start',
    '/auth/google/callback',
    '/auth/google/link',
  ]) {
    const response = await request.get(path, { maxRedirects: 0 })
    expect(response.status(), path).toBe(404)
    // Byte-equal to a path that was never a route: nothing says this one exists.
    expect(await response.text(), path).toBe(await unknown.text())
  }
})
