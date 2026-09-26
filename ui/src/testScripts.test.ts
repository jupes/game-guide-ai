/**
 * testScripts — a shape-pin over `ui/package.json`'s `scripts.test`.
 *
 * `vite.config.ts` defines two vitest projects: `jsdom` (no `testTimeout`
 * override, so vitest's 5000ms default applies) and `storybook`, which
 * launches a real headless Chromium and renders every story plus
 * `addon-a11y` checks. The test that timed out (agent-forge-harness-b4v) is
 * `gm/audioStyles.test.ts` "holds no control character". It is synchronous
 * but not fast: it makes one `expect()` call per character of every Audio*
 * file (about 155k calls), so on its own it takes about 0.6s locally and
 * 1.2-2.7s on a CI runner. Under one `vitest run` the Chromium project
 * competed for the same CPU, the step ran about 1.6-2.0x slower, and the
 * test crossed 5000ms twice (5400ms in run 36068759424, 5065ms in run
 * 36102031804).
 *
 * Running the projects as separate `vitest run` invocations removes that
 * contention. It does not make the test fast: it still spends about half of
 * its 5000ms budget on CI. This test pins the exact script, so a future edit
 * cannot put Chromium back beside jsdom or quietly empty the jsdom run. It
 * follows the convention in `service/tests/test_ci_workflow.py`, ported to
 * TS because the fix lives in `package.json`, not `ci.yml`.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const UI_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')

function readTestScript(): string {
  const pkg = JSON.parse(readFileSync(join(UI_DIR, 'package.json'), 'utf8')) as {
    scripts?: Record<string, string>
  }
  const script = pkg.scripts?.test
  expect(script, 'package.json must define a "test" script').toBeTypeOf('string')
  return script as string
}

describe('package.json scripts.test (agent-forge-harness-b4v)', () => {
  it('is not the bare "vitest run" that shares one process across projects', () => {
    expect(readTestScript()).not.toBe('vitest run')
  })

  it('runs only jsdom, then only storybook in a second process, joined by &&', () => {
    expect(
      readTestScript(),
      'each project needs its own vitest run: jsdom alone first (fails fast), then storybook alone',
    ).toBe('vitest run --project=jsdom && vitest run --project=storybook')
  })
})
