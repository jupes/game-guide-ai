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
 * 2. A confirmed player follows the live seat link on a narrow viewport and
 *    reloads the table. The signed-in context stubs its authorized picture.
 *    A separate signed-out context opens the same URL through the real proxy
 *    and API guard, without the picture stub.
 *
 * Each test signs in with a fresh test account and exercises the normal
 * per-account sign-in limit.
 */

import type { Page } from '@playwright/test'
import type { TableSnapshot } from '../src/gm/contracts'
import { expect, test } from './fixtures'
import { BASE_URL } from './stack'

const PHONE = { width: 390, height: 844 }

test('both entry points advertise the same public install assets', async ({ page, request }) => {
  const path = '/manifest.webmanifest'
  for (const entry of ['/', '/table']) {
    await page.goto(entry)
    await expect(page.locator('link[rel="manifest"]')).toHaveAttribute('href', path)
    await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute('href', '/app-icon-180.png')
  }
  const manifest = await request.get(path)
  expect(manifest.ok()).toBe(true)
  expect(manifest.headers()['content-type']).toBe('application/manifest+json')
  expect(manifest.headers()['set-cookie']).toBeUndefined()
  expect(await manifest.json()).toMatchObject({ id: '/', start_url: '/', scope: '/', display: 'standalone' })
  for (const size of [180, 192, 512]) {
    const icon = await request.get(`/app-icon-${size}.png`)
    expect(icon.ok()).toBe(true)
    expect(icon.headers()['content-type']).toBe('image/png')
    const bytes = await icon.body()
    expect([bytes.readUInt32BE(16), bytes.readUInt32BE(20)]).toEqual([size, size])
  }
})

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

  const installHelp = page.locator('summary').filter({ hasText: 'Add Aetheril to your home screen' })
  await installHelp.click()
  await expect(page.getByText('iPhone:', { exact: true })).toBeVisible()
  await expect(page.getByText('Android:', { exact: true })).toBeVisible()

  // The one real-server state here (no database): both reads fail, with ONE Retry.
  await expect(page.locator('p:not([role])', { hasText: "Couldn't load campaigns" })).toBeVisible()
  await expect(page.locator('p:not([role])', { hasText: "Couldn't load your seats" })).toBeVisible()
  await expect(page.getByRole('button', { name: /^Retry/ })).toHaveCount(1)

  await page.getByRole('button', { name: 'Back to chat' }).click()
  await expect(page).toHaveURL(/\/workspace$/)
})

const SCHEMA_VERSION = 1
const LIVE_CAMPAIGN = `cmp_${'a'.repeat(22)}`
const TABLE_PATH = `/table?campaign_id=${LIVE_CAMPAIGN}`
const PRIVATE_HANDOUT = 'The key under the bell'

function tableSnapshot(shared: string): TableSnapshot {
  return {
    schema_version: SCHEMA_VERSION,
    frames: [
      { schema_version: SCHEMA_VERSION, event: 'session', role: 'participant', audio: false },
      {
        schema_version: SCHEMA_VERSION,
        event: 'snapshot',
        slots: [
          {
            slot: 'table',
            seq: 1,
            content: { content_kind: 'document', type: 'npc', fields: [{ key: 'name', value: shared }] },
          },
          {
            slot: 'mine',
            seq: 1,
            content: { content_kind: 'document', type: 'npc', fields: [{ key: 'name', value: PRIVATE_HANDOUT }] },
          },
        ],
      },
      { schema_version: SCHEMA_VERSION, event: 'ready' },
    ],
  }
}

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

test('a confirmed live seat opens the mobile table, while the same link requires sign-in elsewhere', async ({ page, accounts, browser }) => {
  const seats = [
    seat(LIVE_CAMPAIGN, 'Gorath Table', { live: true, tone: 'Mystery' }),
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

  await page.setViewportSize(PHONE)
  await page.goto('/tavern')
  await submitLogin(page, accounts[1])
  await expect(page).toHaveURL(/\/tavern$/)

  const list = page.getByRole('list', { name: 'Your seats' })
  await expect(list).toBeVisible()
  await expect(list.getByRole('heading', { level: 2 })).toHaveText([
    // Live first; then by when the table last met, else when the seat was taken; concluded last.
    'Gorath Table', 'Lanterns at Low Tide', 'The Pale Orchard', 'The Last Ferry',
  ])
  await expect(list.getByText('Live now')).toHaveCount(1)
  await expect(page.getByRole('group', { name: 'Gorath Table' }).getByText('Live now')).toBeVisible()
  await expect(page.getByRole('group', { name: 'The Pale Orchard' }).getByText('Waiting for your GM to confirm your seat')).toBeVisible()
  await expect(page.getByRole('group', { name: 'The Last Ferry' }).getByText('This table has concluded')).toBeVisible()
  await expect(list.getByRole('button')).toHaveCount(0)
  const join = list.getByRole('link', { name: 'Join table Gorath Table' })
  await expect(list.getByRole('link')).toHaveCount(1)
  await expect(join).toHaveAttribute('href', TABLE_PATH)

  // Pressing the words goes nowhere and asks for nothing.
  const before = requests.length
  await page.getByText('Live now').click()
  await expect(page).toHaveURL(/\/tavern$/)
  expect(requests.slice(before)).toEqual([])
  expect(requests.filter((path) => /^\/(table|join)/.test(path))).toEqual([])
  await expectNoPageOverflow(page)

  let shared = 'The harbour almoner'
  // The deterministic stack serves the built table and keeps realtime off.
  // Stub the authorized picture only in this signed-in browser.
  await page.route(
    (url) => url.pathname === '/table/snapshot' && url.search === `?campaign_id=${LIVE_CAMPAIGN}`,
    (route) => route.fulfill({ json: tableSnapshot(shared) }),
  )
  await join.click()
  await expect(page).toHaveURL(new URL(TABLE_PATH, BASE_URL).href)
  const projection = page.locator('#table-projections')
  await expect(page.getByRole('heading', { name: 'Table', level: 1, exact: true })).toBeVisible()
  await expect(projection.getByRole('heading', { level: 2 })).toHaveText(['For you', 'Shared with the table'])
  await expect(projection.getByText(shared)).toBeVisible()
  await expect(projection.getByText(PRIVATE_HANDOUT)).toBeVisible()
  await expect(page.getByText('Reconnecting… Live updates are delayed.')).toBeVisible()
  await expectNoPageOverflow(page)
  const reload = page.getByRole('button', { name: 'Reload table' })
  await expect(reload).toBeVisible()
  const target = await reload.boundingBox()
  expect(target?.height).toBeGreaterThanOrEqual(44)
  expect(target?.width).toBeGreaterThanOrEqual(44)

  const previous = shared
  shared = 'The ferryman'
  await page.reload()
  await expect(projection.getByText(shared)).toBeVisible()
  await expect(projection.getByText(previous)).toHaveCount(0)

  const signedOut = await browser.newContext({ baseURL: BASE_URL, viewport: PHONE, isMobile: true, hasTouch: true })
  try {
    const visitor = await signedOut.newPage()
    const read = visitor.waitForResponse((response) => new URL(response.url()).pathname === '/table/identity')
    const document = await visitor.goto(TABLE_PATH)
    expect(document?.headers()['referrer-policy']).toBe('no-referrer')
    expect(document?.headers()['cache-control']).toBe('no-store')
    expect((await read).status()).toBe(401)
    await expect(visitor.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible()
    await expect(visitor.locator('#table-projections')).toBeEmpty()
    await expectNoPageOverflow(visitor)
  } finally {
    await signedOut.close()
  }
})
