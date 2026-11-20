import { defineConfig, devices } from '@playwright/test'
import desktopConfig from './playwright.config'

export default defineConfig({
  ...desktopConfig,
  testMatch: ['**/tavern-seats.spec.ts', '**/workbench-composer.spec.ts'],
  outputDir: './e2e-results/mobile',
  reporter: [
    ['list'],
    ['html', { outputFolder: './e2e-results/mobile-html', open: 'never' }],
  ],
  projects: [
    { name: 'mobile-chromium', use: devices['Pixel 5'] },
    { name: 'mobile-webkit', use: devices['iPhone 13'] },
  ],
})
