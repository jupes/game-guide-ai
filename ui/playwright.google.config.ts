import { defineConfig, devices } from '@playwright/test'
import base from './playwright.config'

// Set before Playwright starts the dedicated Compose stack in global setup.
process.env.E2E_FAKE_GOOGLE = '1'

export default defineConfig(base, {
  testIgnore: [],
  testMatch: '**/google-enabled.spec.ts',
  outputDir: './e2e-results/google-tests',
  reporter: [
    ['list'],
    ['html', { outputFolder: './e2e-results/google-html', open: 'never' }],
  ],
  projects: [{ name: 'chromium', use: devices['Desktop Chrome'] }],
})
