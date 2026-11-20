import { defineConfig, devices } from '@playwright/test'

// Each run starts a fresh live-table fixture. The test ends that table.
const webkit = process.env.TABLE_BROWSER_ENGINE === 'webkit'

export default defineConfig({
  testDir: './integration',
  outputDir: `./e2e-results/table-stream-${webkit ? 'webkit' : 'chromium'}`,
  reporter: 'list',
  workers: 1,
  projects: [webkit
    ? { name: 'table-stream-webkit', use: devices['iPhone 13'] }
    : { name: 'table-stream-chromium', use: devices['Pixel 5'] }],
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
