import { randomBytes } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

const PUBLIC = 'Vashti'
const PRIVATE = 'Harbourmistress'
const INACTIVE = 'This table isn’t active or available to you.'

function fixture(name: string): string {
  const value = process.env[`TABLE_BROWSER_${name}`]
  if (!value) throw new Error(`Missing table integration fixture ${name}`)
  return value
}

function projections(page: Page) {
  return page.locator('#table-projections')
}

test('a phone shows only the slots allowed by acceptance and confirmation', async ({ page, context }) => {
  const origin = fixture('PLAYER_URL')
  const path = `${origin}/table?campaign_id=${fixture('CAMPAIGN')}`
  await page.setViewportSize({ width: 320, height: 780 })
  await context.addCookies([{ name: 'gga_session', value: fixture('PENDING_COOKIE'), url: origin + '/table' }])
  await page.goto(path)
  // SEC-41 lets an accepted seat see the table slot. The GM must confirm the
  // seat before it becomes eligible for a personal projection.
  await expect(projections(page)).toContainText(PUBLIC)
  await expect(projections(page)).not.toContainText(PRIVATE)
  for (const role of ['OFFERED', 'OUTSIDER']) {
    await context.addCookies([{ name: 'gga_session', value: fixture(`${role}_COOKIE`), url: origin + '/table' }])
    await page.goto(path)
    await expect(page.locator('#table-status')).toHaveText(INACTIVE)
    await expect(projections(page)).toBeEmpty()
  }
})

test('two mobile clients reconnect, receive Stop, and blank on End across two services', async ({ browser }) => {
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

  async function command(suffix: string, body: object): Promise<void> {
    const response = await fetch(`${play}/campaigns/${fixture('CAMPAIGN')}${suffix}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json', Origin: play, Cookie: `gga_session=${fixture('OWNER_COOKIE')}`,
      },
      body: JSON.stringify({ schema_version: 1, command_id: randomBytes(16).toString('base64url'), ...body }),
    })
    expect(response.status, await response.text()).toBe(200)
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
    await expect(projections(player)).toContainText(PUBLIC)
    await expect(projections(player)).toContainText(PRIVATE)
    const shared = await screenContext.newPage()
    await shared.goto(screen + path)
    await expect(projections(shared)).toContainText(PUBLIC)
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
    await expect(projections(player)).toContainText(PRIVATE)
    await expect(projections(player)).toContainText(PUBLIC)
    await expect.poll(() => Promise.all([connections(play), connections(screen)])).toEqual([1, 1])

    await command('/reveals/stop', { scope: 'document', document_id: fixture('PUBLIC_DOCUMENT') })
    await expect(projections(shared)).toBeEmpty()
    await expect(projections(player)).toContainText(PRIVATE)
    await expect(projections(player)).not.toContainText(PUBLIC)

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
