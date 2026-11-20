import { randomBytes } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

const PUBLIC = 'Vashti'
const PRIVATE = 'Harbourmistress'

function fixture(name: string): string {
  const value = process.env[`TABLE_BROWSER_${name}`]
  if (!value) throw new Error(`Missing table integration fixture ${name}`)
  return value
}

function projections(page: Page) {
  return page.locator('#table-projections')
}

test('two mobile clients reconnect, receive Stop, and blank on End across two services', async ({ browser }) => {
  const play = fixture('PLAYER_URL')
  const screen = fixture('SCREEN_URL')
  const path = `/table?campaign_id=${fixture('CAMPAIGN')}`
  const handset = async (origin: string, width: number, name: string, value: string) => {
    const dimensions = { width, height: 780 }
    const context = await browser.newContext({
      viewport: dimensions, screen: dimensions, isMobile: true, hasTouch: true,
    })
    await context.addCookies([{ name, value, url: origin + '/table' }])
    return context
  }
  const playerContext = await handset(play, 320, 'gga_session', fixture('PLAYER_COOKIE'))
  const screenContext = await handset(screen, 390, 'gga_screen', fixture('SCREEN_COOKIE'))

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
    const player = await playerContext.newPage()
    const shared = await screenContext.newPage()
    await Promise.all([player.goto(play + path), shared.goto(screen + path)])
    await expect(projections(player)).toContainText(PUBLIC)
    await expect(projections(player)).toContainText(PRIVATE)
    await expect(projections(shared)).toContainText(PUBLIC)
    await expect(projections(shared)).not.toContainText(PRIVATE)
    for (const page of [player, shared]) {
      await expect(page.locator('#table-connection')).toBeEmpty()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    }
    await expect.poll(() => Promise.all([connections(play), connections(screen)])).toEqual([1, 1])

    // Hold the new request after clicking Reload, so the test can see the gap
    // before the real service sends the replacement snapshot.
    let resume!: () => void
    const proceed = new Promise<void>((resolve) => { resume = resolve })
    const isStream = (url: URL) => url.pathname === '/table/stream'
    await player.route(isStream, async (route) => { await proceed; await route.continue() }, { times: 1 })
    const request = player.waitForRequest((request) => isStream(new URL(request.url())))
    try {
      await player.getByRole('button', { name: 'Reload table' }).click()
      await request
      await expect(projections(player)).toBeEmpty()
      await expect(projections(shared)).toContainText(PUBLIC)
    } finally {
      resume()
    }
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
      await expect(page.locator('#table-status')).toHaveText('This table isn’t active or available to you.')
    }
    await expect.poll(() => Promise.all([connections(play), connections(screen)])).toEqual([0, 0])
  } finally {
    await Promise.all([playerContext.close(), screenContext.close()])
  }
})
