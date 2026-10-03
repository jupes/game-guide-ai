/**
 * library.spec.ts -- the Campaign Library in the built SPA
 * (agent-forge-harness-1kg.6.4, brief section 8).
 *
 * A GM opens the library from the nav column, searches it, restores an archived NPC,
 * makes a handout by hand, closes the panel with Escape, reopens it from the rail and
 * opens a document, and finally meets it as a full-screen panel on a phone. This stack
 * has no database, so the Workbench's routes are stubbed with `page.route`
 * (`workbenchStubs.ts`); everything else (sign-in, the session, the SPA, the CSP, the
 * guards in fixtures.ts) is real, so the flow is also proof that no remote request, page
 * error or unhandled rejection happened.
 *
 * Signed in as `accounts[1]`: the service budgets 10 auth attempts per account per 5
 * minutes, and the existing specs already spend ten on `accounts[0]`. This spec adds one
 * sign-in beside the two of workbench.spec.ts.
 *
 * GM-private text (a search, a title) never rides in a URL (X-7): the spec asserts that
 * for the search text and for the created document's title.
 */
import { expect, signIn, test } from './fixtures'
import {
  ARCHIVED_DOCUMENT_ID,
  ARCHIVED_DOCUMENT_TITLE,
  CAMPAIGN_ID,
  CREATED_DOCUMENT_ID,
  CREATED_TITLE,
  DOCUMENT_TITLE,
  installWorkbenchRoutes,
} from './workbenchStubs'

const near = (actual: number, expected: number, slack = 2): boolean => Math.abs(actual - expected) <= slack

test('a GM browses, searches, restores and creates in the Campaign Library', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[1])
  const requests: string[] = []
  const bodies: Array<{ path: string; body: Record<string, unknown> }> = []
  await installWorkbenchRoutes(page, { requests, bodies })
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}`)

  const libraryBodies = (): Array<Record<string, unknown>> =>
    bodies.filter((entry) => entry.path === `/campaigns/${CAMPAIGN_ID}/library`).map((entry) => entry.body)
  const panel = page.getByRole('region', { name: 'Campaign Library' })

  // 1 and 2. The nav column's NPCs row opens the panel beside the sidebar, over the chat column.
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'NPCs' }).click()
  await expect(panel).toBeVisible()
  await expect(panel.getByRole('heading', { name: 'Campaign Library', level: 2 })).toBeFocused()
  await expect(panel.getByRole('tab', { name: 'NPCs' })).toHaveAttribute('aria-selected', 'true')
  await expect(panel.getByRole('button', { name: new RegExp(`^${DOCUMENT_TITLE}`) })).toBeVisible()
  expect(libraryBodies()).toHaveLength(1)
  expect(libraryBodies()[0]).toMatchObject({ category: 'npcs', archived: false, search: '' })
  const wide = await panel.boundingBox()
  expect(wide).not.toBeNull()
  expect(near(wide?.x ?? 0, 268)).toBe(true)
  expect(near(wide?.width ?? 0, 320)).toBe(true)

  // 3. A search that matches nothing says so, never reaches a URL, and Clear brings the list back.
  await panel.getByRole('textbox', { name: 'Search NPCs' }).fill('zz')
  await expect(panel.getByText('Nothing matches "zz"')).toBeVisible()
  expect(libraryBodies().at(-1)).toMatchObject({ search: 'zz' })
  expect(requests.filter((line) => line.includes('zz')), 'a search is a request body, never a URL').toEqual([])
  await panel.getByRole('button', { name: 'Clear' }).click()
  await expect(panel.getByRole('button', { name: new RegExp(`^${DOCUMENT_TITLE}`) })).toBeVisible()

  // 4. Archived lists Velka with Restore; Restore makes one call, the row goes, and the status says so.
  await panel.getByRole('button', { name: 'Archived', exact: true }).click()
  await expect(panel.getByRole('button', { name: `Restore ${ARCHIVED_DOCUMENT_TITLE}` })).toBeVisible()
  await panel.getByRole('button', { name: `Restore ${ARCHIVED_DOCUMENT_TITLE}` }).click()
  await expect(panel.getByRole('button', { name: new RegExp(`^${ARCHIVED_DOCUMENT_TITLE}`) })).toBeHidden()
  await expect(panel.getByRole('status')).toHaveText(new RegExp(`Restored ${ARCHIVED_DOCUMENT_TITLE}`))
  expect(requests.filter((line) => line === `POST /campaigns/${CAMPAIGN_ID}/documents/${ARCHIVED_DOCUMENT_ID}/unarchive`)).toHaveLength(1)

  // 5. Documents, New, Player Handout: the canvas opens on the new document, the panel stays beside the rail.
  await panel.getByRole('tab', { name: 'Documents' }).click()
  await panel.getByRole('button', { name: 'New', exact: true }).click()
  await panel.getByRole('button', { name: 'Player Handout' }).click()
  const created = bodies.find((entry) => entry.path === `/campaigns/${CAMPAIGN_ID}/documents`)
  expect(created?.body).toMatchObject({ type: 'handout', data: { name: CREATED_TITLE } })
  expect(String(created?.body.command_id)).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
  const heading = page.getByRole('heading', { name: CREATED_TITLE, level: 2 })
  await expect(heading).toBeVisible()
  await expect(heading).toBeFocused()
  await expect(page).toHaveURL(new RegExp(`document=${CREATED_DOCUMENT_ID}`))
  expect(page.url()).not.toContain('Untitled')
  await expect(panel).toBeVisible()
  const beside = await panel.boundingBox()
  const canvas = await page.locator('.workbench__canvas').boundingBox()
  expect(near(beside?.x ?? 0, 56)).toBe(true)
  expect((beside?.x ?? 0) + (beside?.width ?? 0)).toBeLessThanOrEqual((canvas?.x ?? 0) + 1)

  // 6. Escape closes the panel and focus lands on the rail's Campaign Library button (the sidebar row is gone).
  await page.keyboard.press('Escape')
  await expect(panel).toBeHidden()
  await expect(page.getByRole('navigation', { name: 'Navigation rail' }).getByRole('button', { name: 'Campaign Library' })).toBeFocused()

  // 7. The rail icon reopens it where it left off (Documents); NPCs, then Ondrey opens in the canvas.
  await page.getByRole('navigation', { name: 'Navigation rail' }).getByRole('button', { name: 'Campaign Library' }).click()
  await expect(panel.getByRole('tab', { name: 'Documents' })).toHaveAttribute('aria-selected', 'true')
  await panel.getByRole('tab', { name: 'NPCs' }).click()
  await panel.getByRole('button', { name: new RegExp(`^${DOCUMENT_TITLE}`) }).click()
  await expect(page.getByRole('heading', { name: DOCUMENT_TITLE, level: 2 })).toBeVisible()
  await panel.getByRole('button', { name: 'Close library' }).click()
  await expect(panel).toBeHidden()

  // 8. A phone: close the canvas, open the drawer, and the panel fills the width; Back closes it.
  await page.setViewportSize({ width: 375, height: 812 })
  await page.getByRole('button', { name: 'Back', exact: true }).click()
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'NPCs' }).click()
  await expect(panel).toBeVisible()
  const phone = await panel.boundingBox()
  expect(phone?.x).toBe(0)
  expect(phone?.width).toBe(375)
  await panel.getByRole('button', { name: 'Back', exact: true }).click()
  await expect(panel).toBeHidden()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  // 9. The library reads by POST and writes only what the GM asked for: a create and a restore.
  const writes = requests.filter((line) => !line.startsWith('GET '))
  const allowed = new Set([
    `POST /campaigns/${CAMPAIGN_ID}/library`,
    `POST /campaigns/${CAMPAIGN_ID}/documents`,
    `POST /campaigns/${CAMPAIGN_ID}/documents/${ARCHIVED_DOCUMENT_ID}/unarchive`,
  ])
  expect(writes.filter((line) => !allowed.has(line)), 'only the library read, the create and the restore').toEqual([])
  expect(writes).toContain(`POST /campaigns/${CAMPAIGN_ID}/documents`)
})
