import { promises as fs } from 'node:fs'
import path from 'node:path'
import { expect, test } from '@playwright/test'
import {
  collectPerformanceMetrics,
  installPerformanceObservers,
  writePerformanceArtifacts,
  type PerformanceBudgets,
} from './performance'

test('production app preserves a conversation and emits bounded performance evidence', async ({
  page,
}) => {
  const externalFontRequests: string[] = []
  await page.emulateMedia({ reducedMotion: 'reduce' })
  page.on('request', (request) => {
    if (/fonts\.(googleapis|gstatic)\.com/.test(request.url())) {
      externalFontRequests.push(request.url())
    }
  })
  await installPerformanceObservers(page)
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Aetheril' })).toBeVisible()
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
  expect(externalFontRequests).toEqual([])

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
