import { defineConfig, devices } from '@playwright/test'
import { BASE_URL } from './e2e/stack'

const mobileSpecs = ['**/phone.spec.ts', '**/tavern-seats.spec.ts', '**/workbench-composer.spec.ts']
export const mobileProjects = [
  { name: 'mobile-chromium', use: devices['Pixel 5'], testMatch: mobileSpecs },
  { name: 'mobile-webkit', use: devices['iPhone 13'], testMatch: mobileSpecs },
]

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',
  outputDir: './e2e-results/test-results',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: './e2e-results/html', open: 'never' }],
  ],
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    ...(process.env.E2E_INCLUDE_MOBILE === '1' ? mobileProjects : []),
  ],
})
