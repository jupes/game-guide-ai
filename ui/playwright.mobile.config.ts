import { defineConfig } from '@playwright/test'
import desktopConfig, { mobileProjects } from './playwright.config'

export default defineConfig({
  ...desktopConfig,
  outputDir: './e2e-results/mobile',
  reporter: [
    ['list'],
    ['html', { outputFolder: './e2e-results/mobile-html', open: 'never' }],
  ],
  projects: mobileProjects,
})
