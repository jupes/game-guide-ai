import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './integration',
  outputDir: './e2e-results/table-stream',
  reporter: 'list',
  workers: 1,
  projects: [{ name: 'table-stream-chromium', use: { ...devices['Desktop Chrome'] } }],
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
