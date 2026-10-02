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

import type { Locator, Page } from '@playwright/test'
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

/** The workspace's clipping boxes, outermost first — same list as
 * `.storybook/viewports.ts`'s `expectWorkspaceFits`. */
const WORKSPACE_CLIPS = ['.workspace-shell', '.workspace-shell__body', '.workspace-shell__main'] as const

/**
 * The workspace fits the viewport, measured where `expectNoPageOverflow`
 * cannot see: `.workspace-shell`, `.workspace-shell__body` and
 * `.workspace-shell__main` are all `overflow: hidden` (WorkspaceShell.css), so
 * anything too wide inside them is clipped before it ever reaches
 * `documentElement.scrollWidth`. Mirrors the Storybook canary
 * (`.storybook/viewports.ts`), against the real stack rather than a story.
 *
 * Each clipping box clips nothing (`scrollWidth` never exceeds `clientWidth`),
 * and the composer, its box and "Send message" sit fully inside the phone
 * viewport — checked whether the drawer is open or closed, since the drawer
 * is a `position: fixed` sibling that could still widen an ancestor's
 * scrollable area.
 */
async function expectWorkspaceFits(page: Page): Promise<void> {
  const clipped = await page.evaluate((selectors: readonly string[]) =>
    selectors.map((selector) => {
      const box = document.querySelector(selector)
      if (box === null) throw new Error(`no ${selector}`)
      return { selector, clipped: box.scrollWidth - box.clientWidth }
    }),
  WORKSPACE_CLIPS)
  expect(clipped).toEqual(WORKSPACE_CLIPS.map((selector) => ({ selector, clipped: 0 })))

  const viewport = page.viewportSize()
  const viewportWidth = viewport === null ? PHONE.width : viewport.width
  const controls: ReadonlyArray<readonly [string, Locator]> = [
    ['composer box', page.locator('.chat-pane__composer')],
    ['composer', page.getByPlaceholder('Ask…')],
    ['Send message', page.getByRole('button', { name: 'Send message', exact: true })],
  ]
  const offScreen: Array<{ name: string; pastLeft: number; pastRight: number }> = []
  for (const [name, locator] of controls) {
    const box = await locator.boundingBox()
    if (box === null) throw new Error(`${name} has no box`)
    offScreen.push({
      name,
      pastLeft: Math.max(0, -box.x),
      pastRight: Math.max(0, box.x + box.width - viewportWidth),
    })
  }
  expect(offScreen).toEqual(controls.map(([name]) => ({ name, pastLeft: 0, pastRight: 0 })))
}

test('a player signs in and asks a question on a 390px phone', async ({ page, accounts }) => {
  const prompt = 'Can I cast a cantrip as a bonus action?'

  await page.goto('/')
  await expectNoPageOverflow(page)
  await signIn(page, accounts[0])
  await expectNoPageOverflow(page)

  await page.getByRole('button', { name: 'Sage', exact: true }).click()

  // Narrow layout: the sidebar is a drawer behind the TopBar's menu button.
  const menu = page.getByRole('button', { name: 'Open navigation', exact: true })
  await expect(menu).toBeVisible()
  await expect(page.getByRole('button', { name: 'New conversation', exact: true })).toBeHidden()

  // Closed: expectNoPageOverflow above only sees the document; the workspace's
  // own clipping boxes (overflow: hidden) need their own check.
  await expectWorkspaceFits(page)

  await menu.click()
  const drawer = page.getByRole('dialog', { name: 'Navigation', exact: true })
  await expect(drawer).toBeVisible()

  // Open: the drawer is a `position: fixed` sibling of `.workspace-shell__main`
  // and must not widen it or push the composer off screen.
  await expectWorkspaceFits(page)

  await drawer.getByRole('button', { name: 'New conversation', exact: true }).click()
  await expect(drawer).toBeHidden()

  await page.getByPlaceholder('Ask…').fill(prompt)
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByText(`E2E sage answer: ${prompt}`)).toBeVisible()

  await expectNoPageOverflow(page)
  await expectWorkspaceFits(page)
  const composer = await page.getByPlaceholder('Ask…').boundingBox()
  expect(composer).not.toBeNull()
  expect((composer?.y ?? Infinity) + (composer?.height ?? 0)).toBeLessThanOrEqual(PHONE.height)
})
