/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vite.dev/config/
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { storybookTest } from '@storybook/addon-vitest/vitest-plugin';
import { playwright } from '@vitest/browser-playwright';
const dirname = typeof __dirname !== 'undefined' ? __dirname : path.dirname(fileURLToPath(import.meta.url));

// More info at: https://storybook.js.org/docs/next/writing-tests/integrations/vitest-addon
export default defineConfig({
  plugins: [react()],
  server: {
    // Dev wiring: same-origin in the browser, no CORS changes to the service.
    // Keep in sync with nginx.conf (the compose-mode proxy) — every service
    // API prefix must appear in BOTH, or the SPA fallback swallows it (cnqf).
    proxy: {
      '/chat': 'http://localhost:8000',
      '/healthz': 'http://localhost:8000',
      '/conversations': 'http://localhost:8000',
      '/metrics': 'http://localhost:8000',
      '/auth': 'http://localhost:8000',
      '/models': 'http://localhost:8000'
    }
  },
  test: {
    // Measured over the jsdom project only: the storybook project renders every
    // story in a real browser, which inflates the number without adding any
    // assertions about behaviour.
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.{test,spec}.{ts,tsx}',
        'src/**/*.stories.tsx',
        'src/test-setup.ts',
        'src/main.tsx',        // the mount point; nothing to assert
        'src/vite-env.d.ts',
      ],
      reporter: ['text-summary', 'lcov'],
      // A RATCHET set just under the value measured when the gate went in, not a
      // target. Raise it when the number rises; never lower it to make a build
      // pass. Statements/lines rather than branches, since the branch number is
      // dominated by defensive `??` chains in the API client.
      thresholds: {
        statements: 85,   // measured 87.76 when introduced
        lines: 88,        // measured 90.62
        functions: 87,    // measured 89.62
        branches: 80,     // measured 83.16
      },
    },
    projects: [{
      extends: true,
      test: {
        name: 'jsdom',
        environment: 'jsdom',
        globals: true,
        include: ['src/**/*.{test,spec}.{ts,tsx}'],
        setupFiles: './src/test-setup.ts'
      }
    }, {
      extends: true,
      plugins: [
      // The plugin will run tests for the stories defined in your Storybook config
      // See options at: https://storybook.js.org/docs/next/writing-tests/integrations/vitest-addon#storybooktest
      storybookTest({
        configDir: path.join(dirname, '.storybook')
      })],
      test: {
        name: 'storybook',
        browser: {
          enabled: true,
          headless: true,
          provider: playwright({}),
          instances: [{
            browser: 'chromium'
          }]
        }
      }
    }]
  }
});
