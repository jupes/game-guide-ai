/**
 * A browser-level cold load of an allowlisted client route, and real
 * Back/Forward navigation (agent-forge-harness-y40).
 *
 * IMPORTANT — what this spec does NOT prove: this stack is served by nginx
 * (`ui/nginx.conf`'s `location / { try_files $uri $uri/ /index.html; }`),
 * which already serves `index.html` for ANY unmatched path. A green result
 * here says nothing about production, where `service/spa_fallback.py` +
 * FastAPI's `StaticFiles` mount is the only thing standing between a deep
 * link and a 404. The test that proves THAT is
 * `service/tests/test_spa_fallback.py`, which drives the allowlist directly
 * against a temporary `ui/dist`, with no nginx involved
 * (docs/adr/client-routing.md has the fact table). This spec instead proves
 * the CLIENT half: the right screen renders on a cold load, and Back behaves
 * like an ordinary in-app navigation rather than leaving the app.
 */

import { expect, test, signIn } from './fixtures'

test('a cold load of /profile renders the profile screen', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[0])

  await page.goto('/profile')
  await expect(page.getByRole('heading', { name: 'Profile' })).toBeVisible()
  await expect(page).toHaveURL(/\/profile$/)

  // Back returns to wherever the browser was before this navigation — the
  // signed-in landing screen reached via signIn() above.
  await page.goBack()
  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toBeVisible()
})

test('a real in-app Back after entering the workspace lands on landing, at "/"', async ({
  page, accounts,
}) => {
  await page.goto('/')
  await signIn(page, accounts[0])

  await page.getByRole('button', { name: 'Enter the Tavern' }).click()
  await expect(page).toHaveURL(/\/workspace$/)

  await page.goBack()
  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toBeVisible()
  await expect(page).toHaveURL(/\/$/)
})
