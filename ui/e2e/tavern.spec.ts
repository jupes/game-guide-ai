/**
 * tavern.spec.ts -- a cold load of /tavern in the built SPA, and the GM
 * channel's way in (agent-forge-harness-74j, brief section 7, I-13, T-23a,
 * T-23b).
 *
 * This E2E stack has no database, so every `/campaigns` request answers 503
 * (`service/campaigns_api.py`, F-13) -- the picker's error state is the one
 * this spec can prove here. Its own database-backed states are jsdom's
 * (`tavernRouting.test.tsx`) and Storybook's (`TavernScreen.stories.tsx`).
 *
 * Deliberately a new file, not an edit to any existing spec: the AC requires
 * every existing `ui/e2e/*.spec.ts` file to stay unedited and green.
 */
import { expect, test, signIn } from './fixtures'

test('a cold load of /tavern renders the campaign screen, its error state and Back to chat', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[0])

  const response = await page.goto('/tavern')
  await expect(page.getByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
  await expect(page).toHaveURL(/\/tavern$/)

  // Canary only (Critic 19): the served page carries the app's security
  // headers. security.spec.ts proves the CSP's exact declared value.
  expect(response?.headers()['content-security-policy']).toBeTruthy()
  expect(response?.headers()['x-content-type-options']).toBe('nosniff')

  const errorLine = page.locator('p:not([role])', { hasText: "Couldn't load campaigns" })
  await expect(errorLine).toBeVisible()
  const retry = page.getByRole('button', { name: 'Retry' })
  await expect(retry).toBeVisible()
  await retry.click()
  await expect(errorLine).toBeVisible()

  await page.reload()
  await expect(page.getByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
  await expect(page).toHaveURL(/\/tavern$/)

  await page.getByRole('button', { name: 'Back to chat' }).click()
  await expect(page).toHaveURL(/\/workspace$/)
})

test("the GM channel's Choose a campaign opens /tavern", async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[0])
  await page.getByRole('button', { name: 'Sage', exact: true }).click()

  const channels = page.getByRole('navigation', { name: 'Channels' })
  await channels.getByRole('button', { name: 'GM' }).click()
  await page.getByRole('button', { name: 'Choose a campaign' }).click()

  await expect(page).toHaveURL(/\/tavern$/)
  await expect(page.getByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()

  await page.goBack()
  await expect(page).toHaveURL(/\/workspace$/)
  await expect(channels.getByRole('button', { name: 'GM' })).toHaveAttribute('aria-pressed', 'true')
})
