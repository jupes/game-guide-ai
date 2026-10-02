/**
 * workbench-reveal.spec.ts -- the GM reveals a document in the built SPA
 * (agent-forge-harness-1kg.7.3, brief section 9).
 *
 * This stack has no database, so the Workbench's reads and the reveal surface are stubbed with
 * `page.route` (`workbenchStubs.ts`, `revealStubs.ts`); everything else (sign-in, the session,
 * the SPA, the CSP, the guards in fixtures.ts) is real. The reveal stubs are stateful: a Confirm
 * sets the table slot live and bumps the epoch, a Stop clears it. The fixtures fail a test on a
 * remote request, a page error or an unhandled rejection, so every flow below is also proof that
 * none occurred.
 *
 * One flow, one sign-in as `accounts[1]`: the service budgets 10 auth attempts per account per
 * 5 minutes, and `workbench.spec.ts` signs that account in twice already.
 *
 * Deliberately a new file: every existing spec stays unedited.
 */
import { expect, signIn, test } from './fixtures'
import { installRevealRoutes, SEAT_ALIAS, SESSION_ID } from './revealStubs'
import { CAMPAIGN_ID, DOCUMENT_ID, DOCUMENT_TITLE, installWorkbenchRoutes } from './workbenchStubs'

const WORKSPACE = `/workspace#campaign=${CAMPAIGN_ID}&document=${DOCUMENT_ID}`
const REVEALS_PATH = `/campaigns/${CAMPAIGN_ID}/reveals`

test('a GM reveals an NPC to the table, backs out, picks a player, and stops', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[1])
  const requests: string[] = []
  const bodies: Array<{ path: string; body: unknown }> = []
  await installWorkbenchRoutes(page, { requests })
  await installRevealRoutes(page, { requests, bodies })
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto(WORKSPACE)

  const confirms = (): Array<{ path: string; body: unknown }> => bodies.filter((entry) => entry.path === REVEALS_PATH)
  const stops = (): Array<{ path: string; body: unknown }> => bodies.filter((entry) => entry.path === `${REVEALS_PATH}/stop`)
  const sheet = page.getByRole('dialog', { name: `Reveal ${DOCUMENT_TITLE}` })
  const open = page.getByRole('button', { name: 'Reveal to party' })

  // 1. The document is open, and only once the picture says so does the header say GM ONLY.
  await expect(page.getByRole('heading', { name: DOCUMENT_TITLE, level: 2 })).toBeVisible()
  await expect(page.getByText('GM ONLY', { exact: true })).toBeVisible()
  await expect(open).toBeVisible()

  // 2. Open the sheet: focus goes into it, the table default is ticked, a warning rides on Wants,
  //    and nothing that is not revealable is offered. Nothing has been sent to /reveals.
  await open.click()
  await expect(sheet).toBeVisible()
  await expect(sheet.getByRole('heading', { name: `Reveal ${DOCUMENT_TITLE}` })).toBeFocused()
  const nameAndVoice = sheet.getByRole('switch', { name: 'Name & voice' })
  await expect(nameAndVoice).toHaveAttribute('aria-checked', 'true')
  await expect(sheet.getByRole('switch', { name: 'Qualifier' })).toHaveAttribute('aria-checked', 'false')
  await expect(sheet.getByRole('switch', { name: 'Wants & leverage' })).toHaveAttribute('aria-checked', 'false')
  await expect(sheet.getByText('Would spoil the lie')).toBeVisible()
  await expect(sheet.getByRole('switch', { name: /true identity/i })).toHaveCount(0)
  await expect(sheet.getByRole('switch', { name: 'Tags' })).toHaveCount(0)
  expect(requests).toContain(`POST /campaigns/${CAMPAIGN_ID}/documents/${DOCUMENT_ID}/seal`)
  expect(requests).toContain(`GET /campaigns/${CAMPAIGN_ID}/participants?limit=50`)
  expect(requests.filter((line) => line === `POST ${REVEALS_PATH}`)).toEqual([])

  // 3. Escape cancels: no Confirm was sent, and focus is back on the control that opened it (AE-26).
  await page.keyboard.press('Escape')
  await expect(sheet).toBeHidden()
  expect(requests.filter((line) => line === `POST ${REVEALS_PATH}`)).toEqual([])
  await expect(open).toBeFocused()

  // 4. Reopen. A chosen player re-seeds the draft to empty (an NPC revealed to a participant opens empty),
  //    says so, and cannot be confirmed; the whole table seeds the default again.
  await open.click()
  await expect(sheet).toBeVisible()
  await sheet.getByRole('radio', { name: 'Chosen players' }).check()
  await sheet.getByRole('checkbox', { name: SEAT_ALIAS }).check()
  await expect(sheet.locator('.gm-reveal__statusline')).toHaveText(`Choices reset for ${SEAT_ALIAS}`)
  await expect(sheet.getByRole('status')).toContainText(`Choices reset for ${SEAT_ALIAS}`)
  await expect(sheet.locator('[role="switch"][aria-checked="true"]')).toHaveCount(0)
  await expect(sheet.getByRole('button', { name: 'Reveal', exact: true })).toBeDisabled()
  await sheet.getByRole('radio', { name: 'Whole table' }).check()
  await expect(nameAndVoice).toHaveAttribute('aria-checked', 'true')

  // 5. Tick Qualifier and Confirm. The request carries keys only, the sealed version, the picture's
  //    session and epoch, and no field text.
  await sheet.getByRole('switch', { name: 'Qualifier' }).click()
  await sheet.getByRole('button', { name: 'Reveal to the table' }).click()
  await expect.poll(() => confirms().length).toBe(1)
  const sent = confirms()[0].body as Record<string, unknown>
  expect(sent.mask).toEqual(['name', 'qualifier', 'voice'])
  expect(sent.audience).toEqual({ kind: 'table' })
  expect(sent.version).toBe(2)
  expect(sent.reveal_epoch).toBe(3)
  expect(sent.session_id).toBe(SESSION_ID)
  expect(sent.document_id).toBe(DOCUMENT_ID)
  const json = JSON.stringify(sent)
  expect(json).not.toContain('Harbour almoner')
  expect(json).not.toContain('Quiet, clipped')

  // 6. The sheet closes. The header says what is live, the badge agrees, a field is marked, and the
  //    one status node speaks the outcome.
  await expect(sheet).toBeHidden()
  await expect(page.locator('.gm-canvas__reveal-message')).toHaveText('Revealed · Name & voice, Qualifier · to the table')
  await expect(page.getByText('REVEALED', { exact: true })).toBeVisible()
  await expect(page.getByText('The table can see this').first()).toBeVisible()
  await expect(page.locator('[data-reveal-announcer]')).toContainText('Shown to the table')

  // 7. Stop showing is sent at once, names the document, carries no epoch, and the header goes back.
  await page.getByRole('button', { name: 'Stop showing', exact: true }).click()
  await expect.poll(() => stops().length).toBe(1)
  const stop = stops()[0].body as Record<string, unknown>
  expect(stop.scope).toBe('document')
  expect(stop.document_id).toBe(DOCUMENT_ID)
  expect(stop).not.toHaveProperty('reveal_epoch')
  await expect(open).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop showing', exact: true })).toHaveCount(0)

  // 8. A phone: on the Canvas view the sheet fills the viewport, with no horizontal scroll.
  await page.setViewportSize({ width: 375, height: 812 })
  await expect(page.getByRole('button', { name: 'Canvas', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await open.click()
  await expect(sheet).toBeVisible()
  const box = await sheet.boundingBox()
  expect(box).not.toBeNull()
  expect(Math.round(box?.width ?? 0)).toBe(375)
  expect(Math.round(box?.height ?? 0)).toBe(812)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await sheet.getByRole('button', { name: 'Cancel' }).click()
  await expect(sheet).toBeHidden()

  // 9. No URL the app requested carries a title, an alias or any field text.
  for (const line of requests) {
    for (const secret of [DOCUMENT_TITLE, SEAT_ALIAS, 'Harbour almoner', 'Quiet, clipped', 'family signet']) {
      expect(line, 'a URL carried GM-private text').not.toContain(secret)
    }
  }
  expect(page.url()).not.toContain(DOCUMENT_TITLE)
})
