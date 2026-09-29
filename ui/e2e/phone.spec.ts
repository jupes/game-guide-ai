/**
 * A player signs in on a phone (agent-forge-harness-0rn; owner decision D-1:
 * every player signs in on a phone at the table).
 *
 * The whole path at 390x844 against the real stack: sign in, enter the
 * workspace, open the navigation drawer, start a conversation from it, and
 * send a message — with no page-level horizontal scroll at any step. Every
 * name uses `exact: true`: Playwright's `name` is a substring match, and
 * conversation buttons are titled by the user's own prompts.
 *
 * It runs inside the one `chromium` project with `test.use`, rather than as a
 * second project that would run every spec twice.
 */

import type { Page } from '@playwright/test'
import { expect, signIn, test } from './fixtures'

const PHONE = { width: 390, height: 844 }

test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

async function expectNoPageOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => {
    const root = document.documentElement
    return root.scrollWidth - root.clientWidth
  })
  expect(overflow).toBeLessThanOrEqual(0)
}

test('a player signs in and asks a question on a 390px phone', async ({ page, accounts }) => {
  const prompt = 'Can I cast a cantrip as a bonus action?'

  await page.goto('/')
  await expectNoPageOverflow(page)
  await signIn(page, accounts[0])
  await expectNoPageOverflow(page)

  await page.getByRole('button', { name: 'Enter the Tavern', exact: true }).click()

  // Narrow layout: the sidebar is a drawer behind the TopBar's menu button.
  const menu = page.getByRole('button', { name: 'Open navigation', exact: true })
  await expect(menu).toBeVisible()
  await expect(page.getByRole('button', { name: 'New conversation', exact: true })).toBeHidden()

  await menu.click()
  const drawer = page.getByRole('dialog', { name: 'Navigation', exact: true })
  await expect(drawer).toBeVisible()

  await drawer.getByRole('button', { name: 'New conversation', exact: true }).click()
  await expect(drawer).toBeHidden()

  await page.getByPlaceholder('Ask…').fill(prompt)
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByText(`E2E sage answer: ${prompt}`)).toBeVisible()

  await expectNoPageOverflow(page)
  const composer = await page.getByPlaceholder('Ask…').boundingBox()
  expect(composer).not.toBeNull()
  expect((composer?.y ?? Infinity) + (composer?.height ?? 0)).toBeLessThanOrEqual(PHONE.height)
})
