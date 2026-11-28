import type { Page } from '@playwright/test'
import { expect, signIn, test, type TesterAccount } from './fixtures'
import { BASE_URL } from './stack'
import { CAMPAIGN_ID, installWorkbenchRoutes } from './workbenchStubs'

const title = 'Lantern at Grey Quay'
const handout = 'The Grey Quay contact'
const directives = '"cache", "storage"'

async function openNavigation(page: Page) {
  const drawer = page.getByRole('button', { name: 'Open navigation', exact: true })
  if (await drawer.isVisible()) await drawer.click()
}

async function prepareSignOut(page: Page, account: TesterAccount) {
  await page.goto('/')
  await signIn(page, account)
  await page.getByRole('button', { name: 'Sage', exact: true }).click()
  await openNavigation(page)
  await page.getByRole('button', { name: 'New conversation', exact: true }).click()
  await page.getByPlaceholder('Ask…').fill(title)
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByText(`E2E sage answer: ${title}`)).toBeVisible()
  await expect.poll(() => hasConversation(page)).toBe(true)
  await page.evaluate(() => {
    localStorage.setItem('rag-chat:conversations', 'Retired conversation alias')
    sessionStorage.setItem('game-guide-ai:profile', 'Retired profile alias')
  })
  await page.context().addCookies([{ name: 'unrelated_pref', value: 'kept', url: BASE_URL }])
  await openNavigation(page)
  await page.getByRole('button', { name: 'Open user menu', exact: true }).click()
  return page.getByRole('group', { name: 'User menu', exact: true })
    .getByRole('button', { name: 'Sign out', exact: true })
}

function hasConversation(page: Page) {
  return page.evaluate(prompt => Object.keys(localStorage).some(key =>
    key.startsWith('game-guide-ai:conversations:') && localStorage.getItem(key)?.includes(prompt),
  ), title)
}

async function expectSignedOut(page: Page) {
  await expect(page.getByText('Sign in to continue')).toBeVisible()
  expect((await page.request.get('/auth/me')).status()).toBe(401)
  await expect.poll(() => hasConversation(page)).toBe(false)
  expect(await page.evaluate(() => [
    localStorage.getItem('rag-chat:conversations'), sessionStorage.getItem('game-guide-ai:profile'),
  ])).toEqual([null, null])
  const cookies = await page.context().cookies()
  expect(cookies.some(c => c.name === 'gga_session')).toBe(false)
  expect(cookies.find(c => c.name === 'unrelated_pref')?.value).toBe('kept')
}

test('ordinary sign-out clears account data and expires only the account cookie through nginx', async ({ page, accounts }) => {
  const signOut = await prepareSignOut(page, accounts[0])
  await page.context().addCookies([{
    name: 'gga_screen', value: 'screen-cookie-sentinel', path: '/table',
    domain: new URL(BASE_URL).hostname, httpOnly: true, sameSite: 'Strict',
  }])
  expect((await page.context().cookies()).some(c => c.name === 'gga_session' && c.httpOnly)).toBe(true)
  const [response] = await Promise.all([page.waitForResponse('**/auth/logout'), signOut.click()])
  expect(response.status()).toBe(200)
  expect(await response.json()).toEqual({ ok: true })
  expect(await response.headerValue('clear-site-data')).toBe(directives)
  await expectSignedOut(page)
  expect((await page.context().cookies()).find(c => c.name === 'gga_screen')?.value).toBe('screen-cookie-sentinel')
})

async function openPeerTable(peer: Page) {
  await peer.clock.install()
  const query = '?campaign_id=cmp_signoutTable'
  await peer.route(`**/table/stream${query}`, route => route.fulfill({ status: 404 }))
  await peer.route(`**/table/snapshot${query}`, route => route.fulfill({ json: { schema_version: 1, frames: [
    { schema_version: 1, event: 'session', role: 'participant', audio: false },
    { schema_version: 1, event: 'snapshot', slots: [
      { slot: 'table', seq: 0, content: null },
      { slot: 'mine', seq: 1, content: { content_kind: 'document', type: 'npc', fields: [{ key: 'name', value: handout }] } },
    ] },
    { schema_version: 1, event: 'ready' },
  ] } }))
  await peer.goto(`/table${query}`)
  await expect(peer.getByRole('heading', { name: handout })).toBeVisible()
  // Suspend the next poll. Only the ordinary sign-out signal can clear this picture.
  await peer.clock.pauseAt(new Date(await peer.evaluate(() => Date.now()) + 100))
  return peer.evaluate(() => {
    const appTabKeys = Object.keys(sessionStorage).filter(key =>
      key.startsWith('game-guide-ai:') || key === 'rag-chat:conversations' || key === 'aetheril-theme',
    )
    const messages: unknown[] = []
    const channel = new BroadcastChannel('aetheril:identity')
    channel.onmessage = event => messages.push(event.data)
    Object.assign(window, { observedIdentityMessages: messages, identityObserver: channel })
    return appTabKeys
  })
}

test('a refused sign-out preserves the account; a confirmed headerless retry clears it and redacts the peer table', async ({ page, accounts, context }) => {
  const signOut = await prepareSignOut(page, accounts[0])
  const peer = await context.newPage()
  const peerAppKeys = await openPeerTable(peer)
  let reply: 'unavailable' | 'malformed' | 'confirmed' = 'unavailable'
  let upstreamDirective: string | undefined
  await page.route('**/auth/logout', async route => {
    if (reply === 'unavailable') return route.fulfill({ status: 503 })
    if (reply === 'malformed') return route.fulfill({ status: 200, contentType: 'text/html', body: '<title>Workbench</title>' })
    const upstream = await route.fetch({ maxRedirects: 0 })
    const headers = upstream.headers()
    upstreamDirective = headers['clear-site-data']
    delete headers['clear-site-data']
    await route.fulfill({ response: upstream, headers })
  })
  for (const refusal of ['unavailable', 'malformed'] as const) {
    reply = refusal
    await Promise.all([page.waitForResponse('**/auth/logout'), signOut.click()])
    await expect(page.getByRole('alert')).toHaveText("Couldn't sign out — please try again.")
    expect((await page.request.get('/auth/me')).status()).toBe(200)
    expect(await hasConversation(page)).toBe(true)
    expect(await page.evaluate(() => sessionStorage.getItem('game-guide-ai:profile'))).toBe('Retired profile alias')
    await expect(peer.getByRole('heading', { name: handout })).toBeVisible()
    expect(await peer.evaluate(() => Reflect.get(window, 'observedIdentityMessages'))).toEqual([])
  }

  reply = 'confirmed'
  const [response, identity] = await Promise.all([
    page.waitForResponse('**/auth/logout'), peer.waitForResponse('**/table/identity'), signOut.click(),
  ])
  expect(response.status()).toBe(200)
  expect(await response.json()).toEqual({ ok: true })
  expect(upstreamDirective).toBe(directives)
  expect(await response.headerValue('clear-site-data')).toBeNull()
  expect(identity.status()).toBe(401)
  await expectSignedOut(page)
  await expect.poll(() => peer.evaluate(() => Reflect.get(window, 'observedIdentityMessages')))
    .toEqual([{ v: 1, kind: 'identity-changed' }])
  await expect(peer.locator('#table-projections')).toBeEmpty()
  await expect(peer.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible()
  expect(await hasConversation(peer)).toBe(false)
  expect(await peer.evaluate(keys => keys.filter(key => sessionStorage.getItem(key) !== null), peerAppKeys)).toEqual([])
})

test('a retained workspace conceals its portalled seat offer until restore verifies the current account', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[0])
  await installWorkbenchRoutes(page)
  await page.route(`**/campaigns/${CAMPAIGN_ID}/participants`, route =>
    route.fulfill({ json: { schema_version: 1, items: [], next_cursor: null } }))
  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}`)
  await openNavigation(page)
  await page.getByRole('button', { name: 'Participants', exact: true }).click()
  await page.getByRole('button', { name: 'Offer a seat', exact: true }).click()
  const dialog = page.locator('[role="dialog"]').filter({ hasText: 'Offer a seat' })
  await dialog.getByRole('textbox', { name: 'Display name' }).fill('Private draft alias')
  expect(await dialog.evaluate(element => document.body.contains(element) && !element.closest('#root'))).toBe(true)

  // Model a retained history entry whose frozen tab missed the account change.
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })))
  await expect(page.locator('#root')).toBeHidden()
  await expect(dialog).toHaveCount(1)
  await expect(dialog).toBeHidden()
  const logout = await page.request.post('/auth/logout', { headers: { Origin: new URL(BASE_URL).origin } })
  expect(logout.status()).toBe(200)
  let release!: () => void
  const hold = new Promise<void>(resolve => { release = resolve })
  await page.route('**/auth/me', async route => { await hold; await route.continue() })
  await Promise.all([
    page.waitForRequest('**/auth/me'),
    page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))),
  ])
  await expect(page.locator('#root')).toBeHidden()
  await expect(dialog).toHaveCount(1)
  await expect(dialog).toBeHidden()
  const verified = page.waitForResponse('**/auth/me')
  release()
  expect((await verified).status()).toBe(401)
  await expect(page.getByText('Sign in to continue')).toBeVisible()
  await expect(dialog).toHaveCount(0)
})
