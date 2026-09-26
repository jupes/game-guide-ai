/**
 * testScripts — a shape-pin over `ui/package.json`'s `scripts.test`.
 *
 * `vite.config.ts` defines two vitest projects that both run under one
 * `vitest run` process: `jsdom` (no `testTimeout` override, so vitest's
 * 5000ms default applies) and `storybook`, which launches a real headless
 * Chromium and renders every story plus `addon-a11y` checks. Running them
 * together under a single `vitest run` lets the Chromium project starve the
 * jsdom worker's CPU slice, which is what turned a synchronous,
 * sub-millisecond test (`gm/audioStyles.test.ts`) into an intermittent
 * 5-second timeout under CI load (agent-forge-harness-b4v).
 *
 * The fix is to run the two projects as separate `vitest run` invocations so
 * neither shares a scheduling pool with the other. This test pins that
 * shape so a future edit can't silently put them back on one process —
 * mirrors the convention in `service/tests/test_ci_workflow.py`, ported to
 * TS since the fix lives in `package.json`, not `ci.yml`.
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

  it('runs the jsdom project before the storybook project, joined by &&', () => {
    const script = readTestScript()
    const jsdomIndex = script.indexOf('--project=jsdom')
    const storybookIndex = script.indexOf('--project=storybook')

    expect(jsdomIndex, `expected "--project=jsdom" somewhere in "${script}"`).toBeGreaterThanOrEqual(0)
    expect(storybookIndex, `expected "--project=storybook" somewhere in "${script}"`).toBeGreaterThanOrEqual(0)
    expect(jsdomIndex, 'jsdom must run before storybook so a jsdom failure fails fast').toBeLessThan(
      storybookIndex,
    )

    const between = script.slice(jsdomIndex, storybookIndex)
    expect(between, 'jsdom and storybook runs must be joined by &&, not another operator').toContain('&&')
  })
})
