/**
 * tavern-seats.spec.ts -- the tavern's second half in the built SPA
 * (agent-forge-harness-30c, PR-2): "sign-in returns you where you were" and
 * "Your seats".
 *
 * This E2E stack has no database, so the real server answers `/campaigns` and
 * `/seats` with a 503, and every account in it is a GM. Two tests, then:
 *
 * 1. The real server, no mocks: a signed-out cold load of `/tavern` shows the
 *    sign-in screen, and signing in returns to `/tavern` -- the path only, with
 *    the fragment it was loaded with gone, no new history entry and no focus
 *    move. The 503s are the one real-server state of the seat read: one Retry,
 *    both failure lines.
 * 2. The seat cards, through `page.route` on `/seats` and `/campaigns` only
 *    (precedent: `resilience.spec.ts`; the routed bodies are valid contract
 *    objects, so the real bundle's own parsing reads them): a live table is
 *    plain text and nothing in a seat card is a link or a button; an unconfirmed
 *    seat waits on its GM; the screen fits a 390px phone. The player's own view
 *    (no seat, E-1) needs a `player` account the stack does not seed, so it is
 *    jsdom's and Storybook's.
 *
 * A new file, not an edit to an existing spec. Both tests sign in as the SECOND
 * account: the per-account sign-in budget is 10 attempts per 300 s and is
 * deliberately not raised for the E2E stack (docker-compose.e2e.yml).
 */

import type { Page } from '@playwright/test'
import { expect, test } from './fixtures'

const PHONE = { width: 390, height: 844 }

/** Copied from `phone.spec.ts` (which stays unedited). */
async function expectNoPageOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => {
    const root = document.documentElement
    return root.scrollWidth - root.clientWidth
  })
  expect(overflow).toBeLessThanOrEqual(0)
}

/** The Login form, filled and sent. `fixtures.signIn` waits for Landing, which
 * is exactly what a return to the tavern must NOT show. */
async function submitLogin(page: Page, account: { email: string; password: string }): Promise<void> {
  await page.getByLabel('Email').fill(account.email)
  await page.getByLabel('Password').fill(account.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
}

test('a signed-out /tavern shows Login, and signing in returns to /tavern, path only', async ({ page, accounts }) => {
  await page.goto('/tavern#x=1')
  await expect(page).toHaveURL(/\/tavern#x=1$/)
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  const entries = await page.evaluate(() => window.history.length)

  await submitLogin(page, accounts[1])

  const heading = page.getByRole('heading', { name: 'Your Campaigns', level: 1 })
  await expect(heading).toBeVisible()
  // The path returned, and nothing of the fragment; Landing was never shown.
  await expect(page).toHaveURL(/\/tavern$/)
  expect(page.url()).not.toContain('#')
  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toHaveCount(0)
  expect(await page.evaluate(() => window.history.length)).toBe(entries)
  // An arrival by sign-in is a cold one: the heading is not focused.
  await expect(heading).not.toBeFocused()

  // The one real-server state here (no database): both reads fail, with ONE Retry.
  await expect(page.locator('p:not([role])', { hasText: "Couldn't load campaigns" })).toBeVisible()
  await expect(page.locator('p:not([role])', { hasText: "Couldn't load your seats" })).toBeVisible()
  await expect(page.getByRole('button', { name: /^Retry/ })).toHaveCount(1)

  await page.getByRole('button', { name: 'Back to chat' }).click()
  await expect(page).toHaveURL(/\/workspace$/)
})

const SCHEMA_VERSION = 1

function seat(id: string, name: string, over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: SCHEMA_VERSION,
    campaign_id: id,
    campaign_name: name,
    alias: 'Brannoc',
    accepted_at: '2026-09-02T12:00:00Z',
    confirmed: true,
    tone: null,
    game_system: 'dnd5e',
    avatar_icon: 'castle',
    avatar_tone: 'gold',
    concluded: false,
    last_played_at: null,
    live: false,
    ...over,
  }
}

test('Your seats: a live table is plain text, a seat waits on its GM, and nothing in a seat is a link or a button', async ({ page, accounts }) => {
  const seats = [
    seat('cmp_e2es1', 'Gorath Table', { live: true, tone: 'Mystery' }),
    seat('cmp_e2es2', 'The Pale Orchard', { confirmed: false, live: true }),
    seat('cmp_e2es3', 'Lanterns at Low Tide', { last_played_at: '2026-09-04T12:00:00Z' }),
    seat('cmp_e2es4', 'The Last Ferry', { concluded: true }),
  ]
  await page.route((url) => url.pathname === '/seats', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ schema_version: SCHEMA_VERSION, items: seats, next_cursor: null }),
  }))
  await page.route((url) => url.pathname === '/campaigns', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ schema_version: SCHEMA_VERSION, items: [], next_cursor: null }),
  }))
  const requests: string[] = []
  page.on('request', (request) => { requests.push(new URL(request.url()).pathname) })

  await page.goto('/tavern')
  await submitLogin(page, accounts[1])
  await expect(page).toHaveURL(/\/tavern$/)

  const list = page.getByRole('list', { name: 'Your seats' })
  await expect(list).toBeVisible()
  await expect(list.getByRole('heading', { level: 2 })).toHaveText([
    // Live first; then by when the table last met, else when the seat was taken; concluded last.
    'Gorath Table', 'Lanterns at Low Tide', 'The Pale Orchard', 'The Last Ferry',
  ])
  // A confirmed seat at a live table: the words, never a way in (no table page exists).
  await expect(list.getByText('Live now')).toHaveCount(1)
  await expect(page.getByRole('group', { name: 'Gorath Table' }).getByText('Live now')).toBeVisible()
  await expect(page.getByRole('group', { name: 'The Pale Orchard' }).getByText('Waiting for your GM to confirm your seat')).toBeVisible()
  await expect(page.getByRole('group', { name: 'The Last Ferry' }).getByText('This table has concluded')).toBeVisible()
  await expect(list.getByRole('button')).toHaveCount(0)
  await expect(list.getByRole('link')).toHaveCount(0)

  // Pressing the words goes nowhere and asks for nothing.
  const before = requests.length
  await page.getByText('Live now').click()
  await expect(page).toHaveURL(/\/tavern$/)
  expect(requests.slice(before)).toEqual([])
  expect(requests.filter((path) => /^\/(table|join)/.test(path))).toEqual([])

  // The same screen on a 390px phone.
  await page.setViewportSize(PHONE)
  await expect(list).toBeVisible()
  await expectNoPageOverflow(page)
})
