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
 *
 * The storybook half now runs through `scripts/runStorybookTests.ts` (the
 * guard in `scripts/hangGuard.ts`), not a bare `vitest run
 * --project=storybook` (agent-forge-harness-w1e): CI run 36377294380 went
 * silent after 44 of 45 story files for 23 minutes with no per-test timeout
 * firing. The working hypothesis, not reproduced, is that the tab running
 * the story wedged, and with it the in-tab timer that enforces `testTimeout`.
 * The guard is an external, Node-side watchdog that kills the process tree
 * on its own deadline and names the story files that started and never
 * finished. Because this pin now only reaches the wrapper, the exact
 * storybook command and arguments are pinned in `scripts/hangGuard.test.ts`
 * ("the storybook invocation"), and the guard fails a zero exit unless every
 * story file under `src/` started and finished.
 *
 * The last block pins the other half of the jsdom run: its project must go
 * on collecting the `scripts/**` tests, including hangGuard.test.ts.
 */

import { describe, it, expect } from 'vitest'
import { spawnSync } from 'node:child_process'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, sep } from 'node:path'

const UI_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')

function readTestScript(): string {
  const pkg = JSON.parse(readFileSync(join(UI_DIR, 'package.json'), 'utf8')) as {
    scripts?: Record<string, string>
  }
  const script = pkg.scripts?.test
  expect(script, 'package.json must define a "test" script').toBeTypeOf('string')
  return script as string
}

describe('package.json scripts.test (agent-forge-harness-b4v, agent-forge-harness-w1e)', () => {
  it('is not the bare "vitest run" that shares one process across projects', () => {
    expect(readTestScript()).not.toBe('vitest run')
  })

  it('runs jsdom directly, then the storybook project through the hang guard, joined by &&', () => {
    expect(
      readTestScript(),
      'jsdom needs its own vitest run (fails fast, and stays off the Chromium CPU); the storybook run goes ' +
        'through scripts/runStorybookTests.ts so a hung story fails the step instead of the 25-minute job timeout',
    ).toBe('vitest run --project=jsdom && bun run scripts/runStorybookTests.ts')
  })
})

/** Every file vitest itself collects for the jsdom project, as `ui/`-relative,
 * forward-slash paths. It asks the real `vitest list` (with no tests run)
 * instead of reading `vite.config.ts`: evaluating that config in this jsdom
 * worker would also start the storybook plugin's preset loading, and a
 * textual check would accept the glob in a comment, or a glob that no longer
 * matches anything. */
function jsdomProjectFiles(): string[] {
  const run = spawnSync('bun', ['x', 'vitest', 'list', '--project=jsdom', '--filesOnly'], {
    cwd: UI_DIR,
    encoding: 'utf8',
    timeout: 60_000,
  })
  expect(run.error).toBeUndefined()
  expect(run.status, `vitest list failed:\n${run.stderr}`).toBe(0)
  const files: string[] = []
  for (const line of run.stdout.split(/\r?\n/)) {
    const match = /^\[jsdom\] (.+)$/.exec(line.trim())
    if (match) files.push(match[1].split(sep).join('/'))
  }
  return files
}

describe('the jsdom project runs the scripts/** tests (agent-forge-harness-w1e, pinned by agent-forge-harness-8ug)', () => {
  // This pin lives under src/, not scripts/. Dropping the
  // 'scripts/**/*.{test,spec}.ts' include from vite.config.ts stops every
  // scripts/** test from running, including a pin placed beside them, and
  // the full jsdom run still passes (#144 L2, mutant M10: 80 of 80 files
  // instead of 82 of 82, with no failure).
  it('collects every scripts/*.test.ts file, beside the src/** tests', () => {
    const scriptTests = readdirSync(join(UI_DIR, 'scripts'))
      .filter((name) => name.endsWith('.test.ts'))
      .map((name) => `scripts/${name}`)
    expect(scriptTests).toEqual(expect.arrayContaining(['scripts/hangGuard.test.ts', 'scripts/hangGuardReporter.test.ts']))

    const collected = jsdomProjectFiles()
    expect(collected).toContain('src/testScripts.test.ts')
    expect(collected).toEqual(expect.arrayContaining(scriptTests))
  }, 90_000)
})
