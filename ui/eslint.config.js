// For more info, see https://github.com/storybookjs/eslint-plugin-storybook#configuration-flat-config-format
import storybook from "eslint-plugin-storybook";

import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

// `coverage` holds the generated lcov-report — hand-written-looking JavaScript
// that ESLint will happily scan and complain about. It was already gitignored;
// without it here, `bun run test:coverage && bun run lint` reported warnings
// about a vendored report nobody edits. Linting generated output teaches people
// to ignore lint results, which is worse than not linting at all.
export default defineConfig([globalIgnores(['dist', 'storybook-static', 'coverage']), {
  files: ['**/*.{ts,tsx}'],
  extends: [
    js.configs.recommended,
    tseslint.configs.recommended,
    reactHooks.configs.flat.recommended,
    reactRefresh.configs.vite,
  ],
  languageOptions: {
    globals: globals.browser,
  },
}, ...storybook.configs["flat/recommended"]])
