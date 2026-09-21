import { randomUUID } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { expect, test } from './fixtures'
import {
  collectPerformanceMetrics,
  installPerformanceObservers,
  writePerformanceArtifacts,
  type PerformanceBudgets,
} from './performance'

test('production app preserves a conversation and emits bounded performance evidence', async ({
  page,
}) => {
  // Invites are single-use and emails unique, so each ATTEMPT needs its own —
  // otherwise a retry fails at account creation instead of retrying the test —
  // and so does each RUN, or a second `bun run test:e2e` against a stack that
  // is already up fails on the identity the first one registered. `retry`
  // supplies neither; a nonce supplies both. service/e2e_app.py mints any
  // `e2e-invite-…` token on first sight, so it does not have to be seeded by
  // name (it is still single-use once redeemed).
  const nonce = randomUUID().slice(0, 8)
  const invite = `e2e-invite-token-${nonce}`
  const testerEmail = `e2e-tester-${nonce}@example.com`
  await page.emulateMedia({ reducedMotion: 'reduce' })
  // The font guard that used to live here is now the `guards` fixture, which
  // watches EVERY origin (not just Google Fonts) on every spec in this suite.
  await installPerformanceObservers(page)
  // Access is invite-gated (x5bz.2): land on the invite deep-link and create the
  // account, exactly as a real tester does. The token is seeded by
  // service/e2e_app.py (a browser can't guess a randomly minted one).
  await page.goto(`/#invite=${invite}`)

  await expect(page.getByRole('heading', { name: 'Aetheril' })).toBeVisible()
  await page.getByLabel('Email').fill(testerEmail)
  await page.getByLabel('Password').fill('e2e-password-123')
  await page.getByRole('button', { name: 'Create account' }).click()

  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toBeVisible()
  await page.getByRole('button', { name: 'Enter the Tavern' }).click()
  const channels = page.getByRole('navigation', { name: 'Channels' })
  await channels.getByRole('button', { name: 'Spell' }).click()
  await page.getByRole('button', { name: 'New conversation' }).click()

  const prompt = 'How does magic missile work?'
  await page.getByPlaceholder('Ask…').fill(prompt)
  await page.getByRole('button', { name: 'Send message' }).click()
  await expect(page.getByText(`E2E spell answer: ${prompt}`)).toBeVisible()
  await expect(
    page.getByRole('main').getByText(prompt, { exact: true }),
  ).toBeVisible()

  await page.reload()
  await page.getByRole('button', { name: 'Spell' }).click()
  await page.getByRole('button', { name: prompt, exact: true }).click()
  await expect(page.getByText(`E2E spell answer: ${prompt}`)).toBeVisible()

  await page
    .locator('input[type="file"][aria-label="Attach file"]')
    .setInputFiles({
      name: 'session-notes.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('The party carries a silver key.'),
    })
  await expect(page.getByText('session-notes.txt', { exact: true })).toBeVisible()

  const screenshotDirectory = path.resolve(
    '..',
    'docs',
    'forge',
    'reports',
    'assets',
  )
  await fs.mkdir(screenshotDirectory, { recursive: true })
  await page.screenshot({
    path: path.join(screenshotDirectory, 'eiio-e2e-light.png'),
    fullPage: true,
  })
  const darkTheme = page.getByRole('switch', { name: 'Dark theme' })
  await darkTheme.click()
  await expect(darkTheme).toBeChecked()
  await page.screenshot({
    path: path.join(screenshotDirectory, 'eiio-e2e-dark.png'),
    fullPage: true,
  })

  const budgets = JSON.parse(
    await fs.readFile(
      path.resolve('e2e', 'performance-budget.json'),
      'utf-8',
    ),
  ) as PerformanceBudgets
  const metrics = await collectPerformanceMetrics(page)
  const report = await writePerformanceArtifacts(
    metrics,
    budgets,
    path.resolve('e2e-results'),
  )
  expect(
    Object.values(report.metrics).every((metric) => metric.passed),
  ).toBe(true)
  await expect(
    fs.access(path.resolve('e2e-results', 'performance.json')),
  ).resolves.toBeUndefined()
  await expect(
    fs.access(path.resolve('e2e-results', 'performance.md')),
  ).resolves.toBeUndefined()
})
