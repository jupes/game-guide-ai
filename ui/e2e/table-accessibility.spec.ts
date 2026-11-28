import type { Page, Route } from '@playwright/test'
import type { TableSlot, TableSnapshot } from '../src/gm/contracts'
import { expect, test } from './fixtures'

const campaign = 'cmp_accessibleTable'
const query = `?campaign_id=${campaign}`
const changed = 'Your GM shared something new.'
const stopped = 'Your GM stopped sharing something.'

function reveal(slot: TableSlot['slot'], seq: number, name: string | null, wants?: string): TableSlot {
  return {
    slot, seq, content: name === null ? null : {
      content_kind: 'document', type: 'npc',
      fields: [{ key: 'name', value: name }, ...(wants ? [{ key: 'wants', value: wants }] : [])],
    },
  }
}

/** The fixture serves a public picture and, only for a confirmed player, their own slot. */
async function openTable(page: Page, role: 'participant' | 'guest' = 'participant') {
  const state = {
    table: reveal('table', 1, 'Maren the Ferrymaster', 'Find a safe crossing before nightfall.'),
    mine: reveal('mine', 1, 'The Lantern Keeper', 'Meet me on the east bank after sunset.'),
    active: true,
  }
  await page.clock.install()
  await page.addInitScript(() => {
    const messages: string[] = []
    Object.assign(window, { tableMessages: messages })
    // Observe the persistent live region itself. A final text assertion cannot
    // tell whether an identical announcement was delivered a second time.
    new MutationObserver(records => {
      const region = document.getElementById('table-status')
      if (region && records.some(({ target }) => region.contains(target))) messages.push(region.textContent ?? '')
    }).observe(document, { subtree: true, childList: true, characterData: true })
  })
  await page.route(`**/table/stream${query}`, route => route.fulfill({ status: 404 }))
  await page.route(`**/table/presence${query}`, route => route.request().postDataJSON().action === 'open'
    ? route.fulfill({ json: { schema_version: 1, registration: 'r'.repeat(32) } })
    : route.fulfill({ status: 204 }))
  await page.route(`**/table/snapshot${query}`, route => {
    const frames: TableSnapshot['frames'] = state.active ? [
      { schema_version: 1, event: 'session', role, audio: true },
      { schema_version: 1, event: 'snapshot', slots: role === 'participant' ? [state.table, state.mine] : [state.table] },
      { schema_version: 1, event: 'audio', slot: 'ambience', seq: 0, playing: null },
      { schema_version: 1, event: 'audio', slot: 'one_shot', seq: 0, playing: null },
    ] : [{ schema_version: 1, event: 'inactive' }]
    frames.push({ schema_version: 1, event: 'ready' })
    return route.fulfill({ headers: { 'X-Table-Audio-Context': 'c'.repeat(64) }, json: { schema_version: 1, frames } })
  })
  await page.goto(`/table${query}`)
  await expect(page.getByRole('heading', { name: 'Maren the Ferrymaster' })).toBeVisible()
  return state
}

// Advance a normal five-second poll without using Reload, which intentionally clears
// and re-baselines the view. Leave enough time for the live region's second paint.
async function nextPoll(page: Page) {
  const response = page.waitForResponse(res => new URL(res.url()).pathname === '/table/snapshot')
  await page.clock.fastForward(5_000)
  await (await response).finished()
  await page.clock.runFor(200)
}

async function messages(page: Page) {
  return page.evaluate(() => (window as Window & { tableMessages?: string[] }).tableMessages ?? [])
}

test('only visible changes produce a new polite announcement, including repeated handouts', async ({ page }, info) => {
  const state = await openTable(page)
  const notices = async () => (await messages(page)).filter(message => message === changed || message === stopped)
  const live = page.locator('#table-status')
  await expect(live).toHaveRole('status')
  await nextPoll(page)
  expect(await notices()).toEqual([])

  // A new disclosure can have exactly the same authorized projection as the last one.
  state.table = { ...state.table, seq: 2 }
  await nextPoll(page)
  expect(await notices()).toEqual([changed])
  state.table = reveal('table', 3, 'Orrin of the River', 'Lead the party through the spring floods.')
  await nextPoll(page)
  await expect(page.getByRole('heading', { name: 'Orrin of the River' })).toBeVisible()
  expect(await notices()).toEqual([changed, changed])
  state.mine = reveal('mine', 2, 'The Cartographer', 'Recover the chart hidden beneath the old bridge.')
  await nextPoll(page)
  await expect(page.getByRole('heading', { name: 'The Cartographer' })).toBeVisible()
  expect(await notices()).toEqual([changed, changed, changed])
  await page.screenshot({ path: info.outputPath('player-table-reveals.png'), animations: 'disabled', scale: 'css' })

  // One picture arrives with a public replacement and a private Stop.
  state.table = reveal('table', 4, 'The Bridge Warden')
  state.mine = reveal('mine', 3, null)
  await nextPoll(page)
  await expect(page.getByRole('heading', { name: 'For you' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'The Bridge Warden' })).toBeVisible()
  await nextPoll(page)
  expect(await notices()).toEqual([changed, changed, changed, changed])
  state.table = reveal('table', 5, null)
  await nextPoll(page)
  await expect(live).toHaveText(/Nothing to see yet/)
  expect((await messages(page)).join(' ')).not.toMatch(/Maren|Orrin|Cartographer|Warden|east bank|old bridge/)
})

test('announcing the same handout again keeps controls still at 320px with doubled text', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 568 })
  const state = await openTable(page)
  const textSizeStylesheet = '/assets/test-double-text.css'
  await page.route(`**${textSizeStylesheet}`, route => route.fulfill({
    contentType: 'text/css', body: 'html { font-size: 200% }',
  }))
  await page.addStyleTag({ url: textSizeStylesheet })
  await expect(page.locator('html')).toHaveCSS('font-size', '32px')
  const reload = page.getByRole('button', { name: 'Reload table' })
  const top = () => reload.evaluate(button => button.getBoundingClientRect().top)
  state.table = { ...state.table, seq: 2 }
  await nextPoll(page)
  await expect(page.locator('#table-status')).toHaveText(changed)
  const before = await top()
  const blankTurn = page.evaluate(() => new Promise<number>(resolve => {
    const status = document.getElementById('table-status')!
    const observer = new MutationObserver(() => {
      if (status.textContent) return
      observer.disconnect()
      resolve(document.getElementById('table-reload')!.getBoundingClientRect().top)
    })
    observer.observe(status, { childList: true, characterData: true, subtree: true })
  }))
  state.table = { ...state.table, seq: 3 }
  await nextPoll(page)
  expect(await blankTurn).toBeCloseTo(before, 1)
  await expect(page.locator('#table-status')).toHaveText(changed)
  expect(await top()).toBeCloseTo(before, 1)

  state.active = false
  await nextPoll(page)
  await expect(page.getByRole('article')).toHaveCount(0)
  const redacted = await top()
  expect(redacted).toBeLessThan(before)
  await page.clock.runFor(1_000)
  expect(await top()).toBeCloseTo(redacted, 1)
  await expect(page.locator('#table-status')).not.toHaveText(changed)
})

for (const focused of ['sound', 'navigation'] as const) {
  test(`automatic redaction preserves usable focus or moves it from ${focused} to a safe target`, async ({ page }, info) => {
    const state = await openTable(page)
    const heading = page.getByRole('heading', { level: 1 })
    const reload = page.getByRole('button', { name: 'Reload table' })
    const control = focused === 'sound' ? page.getByRole('button', { name: /Tap to enable sound/ }) : reload
    await page.keyboard.press('Tab')
    await control.focus()
    await expect(control).toBeFocused()
    state.active = false
    await nextPoll(page)
    await expect(page.getByRole('region', { name: 'Table sound' })).toBeHidden()
    await expect(page.getByRole('heading', { name: 'Maren the Ferrymaster' })).toHaveCount(0)
    await expect(focused === 'sound' ? heading : reload).toBeFocused()
    if (focused === 'sound') {
      await page.screenshot({ path: info.outputPath('player-table-redaction-focus.png'), animations: 'disabled', scale: 'css' })
    }
  })
}

for (const role of ['participant', 'guest'] as const) {
  test(`a ${role} can retry a failed exit using the keyboard`, async ({ page }) => {
    const state = await openTable(page, role)
    const account = role === 'participant'
    const action = page.getByRole('button', { name: account ? 'Sign out' : 'Leave table' })
    const tavern = page.getByRole('link', { name: /Tavern|your seats/i })
    const signIn = page.getByRole('link', { name: 'Sign in' })
    const requests: Route[] = []
    if (!account) {
      state.mine = reveal('mine', 2, 'Another player’s handout')
      await nextPoll(page)
      await expect(page.getByRole('heading', { name: 'For you' })).toHaveCount(0)
      expect((await messages(page)).join(' ')).not.toMatch(/Another player|Your GM shared|stopped sharing/)
    }
    await page.route(account ? '**/auth/logout' : '**/table/leave', route => { requests.push(route) })
    await action.focus()
    await page.keyboard.press('Enter')
    await expect.poll(() => requests.length).toBe(1)
    await expect(page.getByRole('heading', { name: 'Maren the Ferrymaster' })).toHaveCount(0)
    await requests[0].fulfill({ status: 503 })
    await expect(page.locator('#table-status')).toHaveText(account ? /sign out.+Try again/ : /leave the table.+Try again/)
    await expect(action).toBeEnabled()
    await expect(action).toBeFocused()
    await page.keyboard.press('Enter')
    await expect.poll(() => requests.length).toBe(2)
    await requests[1].fulfill({ status: 204 })
    await expect(action).toBeHidden()
    await expect(account ? signIn : tavern).toBeFocused()
    if (!account) await expect(signIn).toBeHidden()
  })
}

test('finishing sign-out preserves focus moved to another link while the request was pending', async ({ page }) => {
  await openTable(page)
  let response: Route | undefined
  await page.route('**/auth/logout', route => { response = route })
  await page.getByRole('button', { name: 'Sign out' }).focus()
  await page.keyboard.press('Enter')
  await expect.poll(() => Boolean(response)).toBe(true)
  await page.keyboard.press('Tab')
  const tavern = page.getByRole('link', { name: /Tavern|your seats/i })
  await expect(tavern).toBeFocused()
  await response!.fulfill({ status: 204 })
  await expect(page.getByRole('link', { name: 'Sign in' })).toBeVisible()
  await expect(tavern).toBeFocused()
})
