import type { Page, Route } from '@playwright/test'
import { serveCueSound } from './cueSound'
import { expect, test } from './fixtures'

const query = '?campaign_id=cmp_accessibleTable'
const limited = 'Too many requests. Wait a moment and try again.'
const inactive = 'This table is unavailable.'
const handle = 'm'.repeat(32)
const throttle = { status: 429, headers: { 'Retry-After': '25' } }

function snapshot(name: string, started: string) {
  return {
    headers: { 'X-Table-Audio-Context': 'c'.repeat(64) },
    json: { schema_version: 1, frames: [
      { schema_version: 1, event: 'session', role: 'participant', audio: true },
      { schema_version: 1, event: 'snapshot', slots: [
        { slot: 'table', seq: 1, content: null },
        { slot: 'mine', seq: 1, content: { content_kind: 'document', type: 'npc', fields: [{ key: 'name', value: name }] } },
      ] },
      { schema_version: 1, event: 'audio', slot: 'ambience', seq: 1, playing: {
        asset: { handle, kind: 'audio', media_type: 'audio/mpeg', width: null, height: null, duration_ms: 16_020 },
        started_at: started, start_offset_ms: 0, duration_ms: 16_020, loop: true,
      } },
      { schema_version: 1, event: 'audio', slot: 'one_shot', seq: 0, playing: null },
      { schema_version: 1, event: 'ready' },
    ] },
  }
}

async function openPollingTable(page: Page) {
  const state = {
    stream: 'missing' as 'missing' | 'transport-error' | 'limited' | 'inactive',
    snapshot: 'live' as 'live' | 'limited' | 'hold' | 'fresh',
    reads: [] as string[], presence: [] as string[], assets: 0, held: [] as Route[],
  }
  const now = new Date()
  const started = new Date(now.getTime() - 1000).toISOString()
  await page.clock.install({ time: now })
  await page.addInitScript(() => {
    const voices = new Set<HTMLMediaElement>()
    const play = HTMLMediaElement.prototype.play
    HTMLMediaElement.prototype.play = function () { voices.add(this); return play.call(this) }
    Object.defineProperty(window, 'tableMediaPlaying', {
      get: () => [...voices].some(voice => !voice.paused && voice.currentSrc.includes('/table/assets/')),
    })
  })
  await page.route('**/table/identity', route => {
    state.reads.push('identity')
    return route.fulfill({ json: { schema_version: 1, kind: 'account' } })
  })
  await page.route(`**/table/stream${query}`, route => {
    state.reads.push('stream')
    if (state.stream === 'limited') return route.fulfill(throttle)
    if (state.stream === 'transport-error') return route.fulfill({ status: 502 })
    if (state.stream === 'inactive') return route.fulfill({
      status: 404, json: { detail: { code: 'inactive', message: "There's no live table here.", retryable: false } },
    })
    return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
  })
  await page.route(`**/table/snapshot${query}`, route => {
    state.reads.push('snapshot')
    if (state.snapshot === 'hold') { state.held.push(route); return }
    if (state.snapshot === 'limited') return route.fulfill(throttle)
    return route.fulfill(snapshot(state.snapshot === 'fresh' ? 'Current handout' : 'Earlier handout', started))
  })
  await page.route(`**/table/assets/${handle}`, route => { state.assets++; return serveCueSound(route) })
  await page.route(`**/table/presence${query}`, route => {
    const body = route.request().postDataJSON()
    state.presence.push(body.action === 'report' ? body.state : body.action)
    return body.action === 'open'
      ? route.fulfill({ json: { schema_version: 1, registration: 'r'.repeat(32) } })
      : route.fulfill({ status: 204 })
  })
  await page.goto(`/table${query}`)
  // The unregistered-route fallback must still deliver the polling snapshot.
  await expect(page.getByRole('heading', { name: 'Earlier handout' })).toBeVisible()
  return state
}

async function freezeAfterEnablingSound(page: Page, presence: string[]) {
  await page.getByRole('button', { name: /Tap to enable sound/ }).click()
  await expect.poll(() => presence.at(-1)).toBe('listening')
  await expect.poll(() => page.evaluate(() => Reflect.get(window, 'tableMediaPlaying'))).toBe(true)
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now()) + 100))
}

async function expectCleared(page: Page, presence: string[]) {
  await expect(page.locator('#table-projections')).toBeEmpty()
  await expect(page.getByRole('region', { name: 'Table sound' })).toBeHidden()
  await expect.poll(() => page.evaluate(() => Reflect.get(window, 'tableMediaPlaying'))).toBe(false)
  await expect.poll(() => presence.at(-1)).toBe('close')
}

for (const endpoint of ['stream', 'snapshot'] as const) {
  test(`a ${endpoint} rate limit clears the table and holds all reads through reload and page restoration`, async ({ page }, info) => {
    const state = await openPollingTable(page)
    await freezeAfterEnablingSound(page, state.presence)
    const previousReadCount = state.reads.length
    if (endpoint === 'stream') {
      state.stream = 'limited'
      state.snapshot = 'hold'
    } else {
      // A transport failure alone may keep the prior media until its safety timer.
      // The snapshot 429 must silence it immediately on its own.
      state.stream = 'transport-error'
      state.snapshot = 'limited'
    }
    const observed = page.waitForResponse(r => new URL(r.url()).pathname === `/table/${endpoint}` && r.status() === 429)
    await page.clock.fastForward(5000)
    await observed
    await expect(page.locator('#table-status')).toHaveText(limited)
    await expectCleared(page, state.presence)
    expect(state.reads.slice(previousReadCount)).toEqual(endpoint === 'stream' ? ['stream'] : ['stream', 'snapshot'])
    if (endpoint === 'stream') {
      await page.screenshot({ path: info.outputPath('rate-limited.png'), animations: 'disabled', scale: 'css' })
    }
    const reads = [...state.reads], presence = [...state.presence], assets = state.assets
    state.stream = 'missing'
    state.snapshot = 'fresh'
    const reload = page.getByRole('button', { name: 'Reload table' })
    await expect(reload).toHaveAttribute('aria-disabled', 'true')
    await reload.focus()
    await page.keyboard.press('Enter')
    // Exercise foregrounding and this document's retained Back/Forward lifecycle in both engines.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      document.dispatchEvent(new Event('visibilitychange'))
      Reflect.deleteProperty(document, 'visibilityState')
      document.dispatchEvent(new Event('visibilitychange'))
      window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }))
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))
    })
    await page.clock.fastForward(24_999)
    await expect(page.locator('#table-status')).toHaveText(limited)
    expect(state.reads).toEqual(reads)
    expect(state.presence).toEqual(presence)
    expect(state.assets).toBe(assets)
    await page.clock.fastForward(1)
    await page.clock.resume()
    await expect(page.getByRole('heading', { name: 'Current handout' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Earlier handout' })).toHaveCount(0)
    await expect(reload).not.toHaveAttribute('aria-disabled', 'true')
  })
}

test('a definitive stream denial retires playing media and its registration without another poll', async ({ page }, info) => {
  const state = await openPollingTable(page)
  await freezeAfterEnablingSound(page, state.presence)
  const polls = state.reads.filter(read => read === 'snapshot').length
  state.stream = 'inactive'
  state.snapshot = 'hold'
  await page.clock.fastForward(5000)
  await expect(page.locator('#table-status')).toHaveText(inactive)
  await expectCleared(page, state.presence)
  await page.screenshot({ path: info.outputPath('stream-unavailable.png'), animations: 'disabled', scale: 'css' })
  await page.clock.fastForward(30_000)
  expect(state.reads.filter(read => read === 'snapshot')).toHaveLength(polls)
  expect(state.held).toHaveLength(0)
})

test('a late polling response cannot restore the handout after a definitive denial on reload', async ({ page }) => {
  const state = await openPollingTable(page)
  state.snapshot = 'hold'
  await page.clock.fastForward(5000)
  await expect.poll(() => state.held.length).toBe(1)
  state.stream = 'inactive'
  await page.getByRole('button', { name: 'Reload table' }).click()
  await expect(page.locator('#table-status')).toHaveText(inactive)
  await state.held[0].fulfill(snapshot('Superseded handout', new Date().toISOString()))
  await page.clock.runFor(200)
  await expect(page.locator('#table-projections')).toBeEmpty()
  await expect(page.getByRole('region', { name: 'Table sound' })).toBeHidden()
  await expect(page.locator('#table-status')).toHaveText(inactive)
})
