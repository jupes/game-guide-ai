import { randomBytes } from 'node:crypto'
import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import { TableSessionAnswerSchema } from '../src/gm/contracts'

const PUBLIC = 'Vashti'
const PRIVATE = 'Harbourmistress'
const INACTIVE = 'This table is unavailable.'
const LIVE_UPDATE_TIMEOUT = 4_000

function fixture(name: string): string {
  const value = process.env[`TABLE_BROWSER_${name}`]
  if (!value) throw new Error(`Missing table integration fixture ${name}`)
  return value
}

function projections(page: Page) {
  return page.locator('#table-projections')
}

async function accountCookie(context: BrowserContext, origin: string, name: string) {
  await context.addCookies([{
    name: 'gga_session', value: fixture(name), url: origin + '/', httpOnly: true, sameSite: 'Lax',
  }])
}

async function screenCookie(context: BrowserContext, origin: string, name: string) {
  await context.addCookies([{
    name: 'gga_screen', value: fixture(name), domain: new URL(origin).hostname,
    path: '/table', httpOnly: true, sameSite: 'Strict',
  }])
}

test('owners and seated players sign out of their account whatever they can see', async ({ page, context }, info) => {
  const origin = fixture('PLAYER_URL')
  const path = `${origin}/table?campaign_id=${fixture('CAMPAIGN')}`
  await page.setViewportSize({ width: 320, height: 780 })
  for (const account of ['OWNER', 'PENDING', 'PLAYER']) {
    await accountCookie(context, origin, `${account}_COOKIE`)
    await page.goto(path)
    await expect(page.getByRole('heading', { level: 1, name: 'Table', exact: true })).toBeVisible()
    await expect(projections(page)).toContainText(PUBLIC, { timeout: LIVE_UPDATE_TIMEOUT })
    if (account === 'PLAYER') await expect(projections(page)).toContainText(PRIVATE)
    else await expect(projections(page)).not.toContainText(PRIVATE)
    await expect(page.getByRole('link', { name: 'Back to your seats' })).toHaveAttribute('href', '/tavern')
    await expect(page.getByRole('button', { name: 'Leave table' })).toBeHidden()
    expect((await context.request.get(origin + '/auth/me')).status()).toBe(200)
    if (account === 'PENDING') {
      await page.screenshot({ path: info.outputPath('table-only-account.png'), animations: 'disabled', scale: 'css' })
    }
    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible()
    expect((await context.request.get(origin + '/auth/me')).status()).toBe(401)
  }
})

test('cold inactive, missing and full addresses keep account controls without disclosing a campaign', async ({ page, context }, info) => {
  const origin = fixture('PLAYER_URL')
  const path = `${origin}/table?campaign_id=${fixture('CAMPAIGN')}`
  const unknown = `${origin}/table?campaign_id=cmp_${'z'.repeat(22)}`
  const signOut = page.getByRole('button', { name: 'Sign out' })
  const status = page.locator('#table-status')
  for (const role of ['OFFERED', 'OUTSIDER']) {
    await accountCookie(context, origin, `${role}_COOKIE`)
    for (const address of [path, unknown, origin + '/table']) {
      await page.goto(address)
      await expect(status).toHaveText(INACTIVE)
      await expect(projections(page)).toBeEmpty()
      await expect(signOut).toBeVisible()
      await expect(page.getByRole('link', { name: 'Back to your seats' })).toHaveAttribute('href', '/tavern')
    }
  }
  await page.screenshot({ path: info.outputPath('unavailable-account.png'), animations: 'disabled', scale: 'css' })
  // Substitute only the per-instance capacity response. The credential lookup
  // and the exit still run through the real services and their cookie jar.
  await accountCookie(context, origin, 'OWNER_COOKIE')
  await page.route('**/table/stream?*', route => route.fulfill({ status: 503, headers: { 'Retry-After': '30' } }))
  await page.goto(path)
  await expect(status).toHaveText('This table is full. Ask your GM to make room.')
  await page.screenshot({ path: info.outputPath('full-account.png'), animations: 'disabled', scale: 'css' })
  await signOut.click()
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible()
  expect((await context.request.get(origin + '/auth/me')).status()).toBe(401)
})

test('Leave revokes the room screen across services and preserves the account in the same browser', async ({ browser, context, page }, info) => {
  const origin = fixture('SCREEN_URL')
  const otherService = fixture('PLAYER_URL')
  const campaign = fixture('CAMPAIGN')
  const path = `${origin}/table?campaign_id=${campaign}`
  const leave = page.getByRole('button', { name: 'Leave table' })
  await screenCookie(context, origin, 'EXIT_SCREEN_COOKIE')
  await accountCookie(context, origin, 'PLAYER_COOKIE')

  for (const address of [`${origin}/table?campaign_id=cmp_${'z'.repeat(22)}`, origin + '/table']) {
    await page.goto(address)
    await expect(page.locator('#table-status')).toHaveText(INACTIVE)
    await expect(leave).toBeVisible()
    await expect(projections(page)).toBeEmpty()
  }
  await page.route('**/table/stream?*', route => route.fulfill({ status: 503, headers: { 'Retry-After': '30' } }))
  await page.goto(path)
  await expect(page.locator('#table-status')).toHaveText('This table is full. Ask your GM to make room.')
  await expect(leave).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeHidden()
  await page.unroute('**/table/stream?*')
  await page.goto(path)
  await expect(projections(page)).toContainText(PUBLIC, { timeout: LIVE_UPDATE_TIMEOUT })
  await expect(projections(page)).not.toContainText(PRIVATE)
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeHidden()
  await expect(page.getByRole('link', { name: 'Back to the Tavern' })).toHaveAttribute('href', '/tavern')
  await page.screenshot({ path: info.outputPath('shared-screen.png'), animations: 'disabled', scale: 'css' })

  // A fresh context can still use the same screen grant on the other service.
  // Its later refusal proves revocation in the database, beyond clearing this page's cookie.
  const screenOnly = await browser.newContext()
  try {
    await screenCookie(screenOnly, otherService, 'EXIT_SCREEN_COOKIE')
    const snapshot = `${otherService}/table/snapshot?campaign_id=${campaign}`
    expect((await screenOnly.request.get(snapshot)).status()).toBe(200)
    // A screen could arrive after the app's last account-identity read.
    // Mutation-time precedence keeps both credentials valid for the intended exit.
    const changed = await context.request.post(origin + '/table/sign-out', { headers: { Origin: origin } })
    expect(changed.status()).toBe(409)
    expect((await context.request.get(origin + '/auth/me')).status()).toBe(200)
    expect((await screenOnly.request.get(snapshot)).status()).toBe(200)
    expect(await (await context.request.get(origin + '/table/identity')).json()).toEqual({ schema_version: 1, kind: 'screen' })
    await leave.click()
    await expect(page.locator('#table-status')).toHaveText('You left the table.')
    await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeHidden()
    expect((await screenOnly.request.get(snapshot)).status()).toBe(401)
    expect((await context.request.get(origin + '/auth/me')).status()).toBe(200)
    expect(await (await context.request.get(origin + '/table/identity')).json()).toEqual({ schema_version: 1, kind: 'account' })
  } finally {
    await screenOnly.close()
  }
})

test('two mobile clients handle Stop, Rotate, and End across two services', async ({ browser }) => {
  const play = fixture('PLAYER_URL')
  const screen = fixture('SCREEN_URL')
  const path = `/table?campaign_id=${fixture('CAMPAIGN')}`
  const handset = async (device: typeof browser, origin: string, width: number, name: string, value: string) => {
    const dimensions = { width, height: 780 }
    const context = await device.newContext({
      viewport: dimensions, screen: dimensions, isMobile: true, hasTouch: true,
    })
    await context.addCookies([{ name, value, url: origin + '/table' }])
    return context
  }
  const playerContext = await handset(browser, play, 320, 'gga_session', fixture('PLAYER_COOKIE'))
  // Each device has a foreground page, including in engines that suspend
  // the background page of a second context in the same browser process.
  const displayBrowser = await browser.browserType().launch()

  async function command(suffix: string, body: object, origin = play): Promise<unknown> {
    const response = await fetch(`${origin}/campaigns/${fixture('CAMPAIGN')}${suffix}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json', Origin: origin, Cookie: `gga_session=${fixture('OWNER_COOKIE')}`,
      },
      body: JSON.stringify({ schema_version: 1, command_id: randomBytes(16).toString('base64url'), ...body }),
    })
    const result = await response.text()
    expect(response.status, result).toBe(200)
    return JSON.parse(result)
  }

  const connections = async (origin: string) => {
    const response = await fetch(`${origin}/healthz`)
    const body = await response.json() as { realtime?: { streams: number } }
    return body.realtime?.streams
  }

  try {
    const screenContext = await handset(displayBrowser, screen, 390, 'gga_screen', fixture('SCREEN_COOKIE'))
    const player = await playerContext.newPage()
    await player.goto(play + path)
    await expect(projections(player)).toContainText(PUBLIC, { timeout: LIVE_UPDATE_TIMEOUT })
    await expect(projections(player)).toContainText(PRIVATE)
    const shared = await screenContext.newPage()
    await shared.goto(screen + path)
    await expect(projections(shared)).toContainText(PUBLIC, { timeout: LIVE_UPDATE_TIMEOUT })
    await expect(shared.getByRole('button', { name: 'Leave table' })).toBeVisible()
    await expect(projections(shared)).not.toContainText(PRIVATE)
    for (const page of [player, shared]) {
      await expect(page.locator('#table-connection')).toBeEmpty()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    }
    await expect.poll(() => Promise.all([connections(play), connections(screen)])).toEqual([1, 1])

    // Check the DOM in the same browser task as the click. The new stream can
    // then deliver its real response without a request interceptor.
    const request = player.waitForRequest((request) => new URL(request.url()).pathname === '/table/stream')
    const blanked = await player.getByRole('button', { name: 'Reload table' }).evaluate((button: HTMLButtonElement) => {
      button.click()
      return document.querySelector('#table-projections')?.childElementCount === 0
    })
    expect(blanked).toBe(true)
    await request
    await expect(projections(shared)).toContainText(PUBLIC)
    await expect(projections(player)).toContainText(PRIVATE, { timeout: LIVE_UPDATE_TIMEOUT })
    await expect(projections(player)).toContainText(PUBLIC)
    await expect.poll(() => Promise.all([connections(play), connections(screen)])).toEqual([1, 1])

    await command('/reveals/stop', { scope: 'document', document_id: fixture('PUBLIC_DOCUMENT') })
    await expect(projections(shared)).toBeEmpty({ timeout: LIVE_UPDATE_TIMEOUT })
    await expect(projections(player)).toContainText(PRIVATE)
    await expect(projections(player)).not.toContainText(PUBLIC, { timeout: LIVE_UPDATE_TIMEOUT })

    const reopened = player.waitForRequest((req) => new URL(req.url()).pathname === '/table/stream')
    const { session } = TableSessionAnswerSchema.parse(await command('/table-session', {
      action: 'rotate', session_id: fixture('SESSION'),
    }, screen))
    if (!session) throw new Error('Rotate returned no live session')
    await reopened
    await expect(player.locator('#table-status')).toHaveText('Nothing to see yet. Your GM hasn’t shared anything.')
    await expect(projections(player)).toBeEmpty()
    await expect(shared.locator('#table-status')).toHaveText(INACTIVE)
    await expect(projections(shared)).toBeEmpty()
    await expect(shared.getByRole('link', { name: 'Sign in', exact: true })).toBeHidden()
    await expect(shared.getByRole('button', { name: 'Leave table' })).toBeHidden()
    await expect(shared.getByRole('button', { name: 'Sign out' })).toBeHidden()
    await expect.poll(() => Promise.all([connections(play), connections(screen)])).toEqual([1, 0])
    const revoked = await screenContext.request.get(`${screen}/table/snapshot?campaign_id=${fixture('CAMPAIGN')}`)
    expect(revoked.status()).toBe(401)

    await command('/reveals', {
      session_id: session.session_id, reveal_epoch: session.reveal_epoch,
      document_id: fixture('PUBLIC_DOCUMENT'), version: 1, mask: ['name'], audience: { kind: 'table' },
    }, screen)
    await expect(projections(player)).toContainText(PUBLIC, { timeout: LIVE_UPDATE_TIMEOUT })
    await expect(projections(player)).not.toContainText(PRIVATE)
    await expect(projections(shared)).toBeEmpty()

    await command('/table-session', { action: 'end', session_id: fixture('SESSION') })
    for (const page of [player, shared]) {
      await expect(projections(page)).toBeEmpty()
      await expect(page.locator('#table-status')).toHaveText(INACTIVE)
    }
    await expect.poll(() => Promise.all([connections(play), connections(screen)])).toEqual([0, 0])
  } finally {
    await Promise.all([playerContext.close(), displayBrowser.close()])
  }
})
