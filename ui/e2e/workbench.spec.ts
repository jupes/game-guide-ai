/**
 * workbench.spec.ts -- the Workbench screen in the built SPA
 * (agent-forge-harness-1kg.6.3, brief section 8, Critic C-19).
 *
 * This stack has no database, so the Workbench's reads are stubbed with `page.route`
 * (`workbenchStubs.ts`); everything else (sign-in, the session, the SPA, the CSP, the
 * guards in fixtures.ts) is real. The fixtures fail a test on a remote request, a page
 * error or an unhandled rejection, so every flow below is also proof that none occurred.
 *
 * Every e2e invite mints a `dm` account, so "a player never sees the Workbench" cannot
 * be shown here; it is `WorkspaceShellWorkbench.test.tsx`'s.
 *
 * Both tests sign in as `accounts[1]`, not `accounts[0]`: the service budgets 10 auth attempts
 * per account per 5 minutes (`AUTH_RATE_LIMIT_PER_ACCOUNT`), and the existing specs already spend
 * ten of them on `accounts[0]`, so a spec that signs it in again fails with a 429 that reads like
 * a product fault.
 *
 * Deliberately a new file: every existing spec stays unedited.
 */
import { expect, signIn, test } from './fixtures'
import {
  CAMPAIGN_ID,
  DOCUMENT_ID,
  DOCUMENT_TITLE,
  MISSING_DOCUMENT_ID,
  installWorkbenchRoutes,
} from './workbenchStubs'

const WORKSPACE = `/workspace#campaign=${CAMPAIGN_ID}&document=${DOCUMENT_ID}`

test('a GM opens the Workbench, a document stays open, and the layout follows the width', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[1])
  const requests: string[] = []
  await installWorkbenchRoutes(page, { requests })
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto(WORKSPACE)

  // Wide, with a document: the rail, the chat (472px) and the canvas beside it.
  const heading = page.getByRole('heading', { name: DOCUMENT_TITLE, level: 2 })
  await expect(heading).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Navigation rail' })).toBeVisible()
  const conversation = page.getByRole('region', { name: 'Conversation' })
  await expect(conversation).toBeVisible()
  const chatBox = await page.locator('.workbench__chat').boundingBox()
  const canvasBox = await page.locator('.workbench__canvas').boundingBox()
  expect(chatBox).not.toBeNull()
  expect(canvasBox).not.toBeNull()
  if (chatBox === null || canvasBox === null) return
  expect(chatBox.x + chatBox.width).toBeLessThanOrEqual(canvasBox.x + 1)
  expect(Math.abs(chatBox.width - 472)).toBeLessThanOrEqual(1)

  // Closing brings the sidebar back; the URL forgets the document.
  await page.getByRole('button', { name: 'Close canvas' }).click()
  const sidebar = page.getByRole('navigation', { name: 'Main navigation' })
  await expect(sidebar).toBeVisible()
  expect(Math.round((await sidebar.boundingBox())?.width ?? 0)).toBe(268)
  await expect(page).not.toHaveURL(/document=/)

  // Opening it again through the Campaign Library (1kg.6.4 replaced the nav column's placeholder list):
  // focus lands on the heading, and the URL carries an id, never a title.
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'NPCs' }).click()
  await page
    .getByRole('region', { name: 'Campaign Library' })
    .getByRole('button', { name: new RegExp(`^${DOCUMENT_TITLE}`) })
    .click()
  await expect(heading).toBeVisible()
  await expect(heading).toBeFocused()
  await expect(page).toHaveURL(new RegExp(`document=${DOCUMENT_ID}`))
  expect(page.url()).not.toContain(DOCUMENT_TITLE)

  // The version history: two rows, one request for them.
  await page.getByRole('button', { name: 'v2 — version history' }).click()
  await expect(page.getByRole('region', { name: 'Version history' }).getByRole('listitem')).toHaveCount(2)
  expect(requests.filter((line) => line.includes('/versions'))).toHaveLength(1)

  // A reload restores it from the fragment, with no tool or chat request and nothing but GETs and the library read.
  await page.reload()
  await expect(heading).toBeVisible()
  expect(
    requests.filter((line) => !line.startsWith('GET ') && !line.endsWith('/library')),
    'the Workbench reads; it never writes',
  ).toEqual([])

  // A phone: the switch, the Canvas view, and no horizontal scroll.
  await page.setViewportSize({ width: 375, height: 812 })
  await expect(page.getByRole('group', { name: 'Workbench view' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Canvas', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(heading).toBeVisible()
  await page.getByRole('button', { name: 'Chat', exact: true }).click()
  await expect(conversation).toBeVisible()
  await expect(heading).toBeHidden()
  await page.getByRole('button', { name: 'Canvas', exact: true }).click()
  await expect(heading).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  // A tablet: the rail and the switch, one column.
  await page.setViewportSize({ width: 900, height: 1024 })
  await expect(page.getByRole('navigation', { name: 'Navigation rail' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'Workbench view' })).toBeVisible()
  await expect(heading).toBeVisible()
  await expect(conversation).toBeHidden()
})

test('an unavailable document says so without saying why, and Close clears it', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[1])
  await installWorkbenchRoutes(page)
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}&document=${MISSING_DOCUMENT_ID}`)

  await expect(page.getByRole('heading', { name: "This document isn't available", level: 2 })).toBeVisible()
  await expect(page.getByText('It may have been deleted, or you may not have access to it.')).toBeVisible()
  await page.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(page).not.toHaveURL(/document=/)
  await expect(page.getByRole('heading', { name: "This document isn't available", level: 2 })).toBeHidden()
})
