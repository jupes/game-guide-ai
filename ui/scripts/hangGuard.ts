/**
 * hangGuard (agent-forge-harness-w1e) — a wall-clock supervisor for the
 * Storybook browser test project, so a hung story fails the CI step directly
 * instead of riding out to the `ui-tests` job's 25-minute timeout, and so a
 * storybook run that quietly ran nothing cannot pass.
 *
 * ## Why Vitest's own `testTimeout` may not be enough here
 *
 * Vitest wraps every test's handler in `withTimeout`: a `Promise.race`
 * against a `setTimeout` (see `@vitest/runner`'s `chunk-artifact.js`). For the
 * `storybook` project that timer runs *inside the browser tab* — the test
 * file, `@storybook/addon-vitest`'s `testStory` wrapper and the timeout race
 * itself are all bundled into the page and executed there; Node only
 * receives the result over the browser bridge once the page reports it. A
 * tab whose JS thread is wedged (a synchronous loop, say) never runs that
 * timer either, and Node waits for a message that is not coming.
 *
 * That is a hypothesis for CI run 36377294380, not a reproduction of it: that
 * run printed 44 of its 45 story files and then went silent until the job cap
 * cancelled it, with no "Test timed out" error. A deliberately wedged story
 * reproduces the silence and the missing timeout locally, but not reliably
 * the 44-of-45 shape (one local run let the other 44 files finish, another
 * printed nothing for any file), and a lost CDP connection or a stall on the
 * Node side would look the same from out here. The guard below does not
 * depend on which it was.
 *
 * ## What this does instead
 *
 * It runs the storybook `vitest` invocation (`STORYBOOK_COMMAND` +
 * `storybookVitestArgs`, both pinned by `hangGuard.test.ts`) as a child
 * process, tees its output to our own stdout/stderr, and races the whole run
 * against an external, Node-side timer. The child also loads
 * `scripts/hangGuardReporter.ts`, which appends a `start`/`end` line per story
 * file to an events file this guard hands it through `HANG_GUARD_EVENTS_FILE`.
 *
 * - On the deadline, it force-kills the *process tree* — which does not need
 *   the browser tab's cooperation — and names the story files that started
 *   and never finished (the likely hang) separately from the ones that never
 *   started. Console output that happens to contain a story's path does not
 *   count as a result.
 * - On a zero exit, it still fails unless every expected story file started
 *   and finished (and fails an empty expected list), so a wrong `--project`
 *   or a reporter that never loaded cannot turn into a silent green.
 * - A signal death (exit code `null`) or a launch error fails too.
 */

import { spawn, execFileSync, type ChildProcess, type SpawnOptions } from 'node:child_process'
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, sep } from 'node:path'
import { platform, tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { HANG_GUARD_EVENTS_ENV } from './hangGuardReporter'

export { HANG_GUARD_EVENTS_ENV }

/** The program that runs the storybook project. */
export const STORYBOOK_COMMAND = 'bunx'

/** The reporter module, as vitest resolves it from `ui/` (the child's cwd). */
export const HANG_GUARD_REPORTER = './scripts/hangGuardReporter.ts'

/**
 * The storybook project's `vitest` arguments. Naming any `--reporter` on the
 * command line replaces vitest's defaults, including the `github-actions`
 * reporter it would otherwise add on its own when `GITHUB_ACTIONS=true`, so
 * that one is named here too.
 */
export function storybookVitestArgs(env: NodeJS.ProcessEnv): string[] {
  return [
    'vitest',
    'run',
    '--project=storybook',
    '--reporter=default',
    ...(env.GITHUB_ACTIONS === 'true' ? ['--reporter=github-actions'] : []),
    `--reporter=${HANG_GUARD_REPORTER}`,
  ]
}

/** The one `spawn` signature the guard calls; the real `child_process.spawn` satisfies it. */
export type SpawnFn = (command: string, args: readonly string[], options: SpawnOptions) => ChildProcess

export interface HangGuardResult {
  /** The process exit code to propagate (0 only on a clean, complete pass). */
  code: number
  timedOut: boolean
  /** Story files that started and never finished, sorted. On a timeout, these are the likely hang. */
  inFlightStoryFiles: string[]
  /** Expected story files that never started, in `expectedStoryFiles` order. */
  neverStartedStoryFiles: string[]
}

export interface HangGuardOptions {
  command: string
  args: string[]
  cwd: string
  timeoutMs: number
  /** Every story file the run must start and finish, relative to `cwd`, forward-slash separated. */
  expectedStoryFiles: readonly string[]
  /** The child's environment, before the events-file variable is added. Defaults to `process.env`. */
  env?: NodeJS.ProcessEnv
  /** Test seam: defaults to the real `child_process.spawn`. */
  spawnFn?: SpawnFn
  /** Test seam: defaults to `process.stdout`. */
  stdout?: NodeJS.WritableStream
  /** Test seam: defaults to `process.stderr`. */
  stderr?: NodeJS.WritableStream
  /** Test seam: defaults to the real, OS-level process-tree kill. */
  killFn?: (pid: number) => void
}

/**
 * Kills the whole process tree rooted at `pid`, not just the immediate
 * child — a stuck story leaves the vitest process's own children (esbuild,
 * the headless browser) running, and those are what keep a naive SIGTERM
 * from actually freeing the CPU/CI runner.
 */
export function killProcessTree(pid: number): void {
  if (platform() === 'win32') {
    try {
      execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'])
    } catch {
      // Best effort: the process may already be gone.
    }
    return
  }
  try {
    // Negative pid signals the whole process group (see the `detached: true`
    // spawn option below, which puts the child in its own group).
    process.kill(-pid, 'SIGKILL')
  } catch {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      // Already gone.
    }
  }
}

/** Recursively lists `*.stories.tsx` files under `rootDir`, relative to it, forward-slash separated. No new dependency: this is the one thing a glob library would buy us, and the tree is shallow. */
export function findStoryFiles(rootDir: string): string[] {
  const results: string[] = []
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue
      const full = join(dir, entry.name)
      if (entry.isDirectory()) {
        walk(full)
      } else if (entry.isFile() && entry.name.endsWith('.stories.tsx')) {
        results.push(relative(rootDir, full).split(sep).join('/'))
      }
    }
  }
  walk(rootDir)
  return results.sort()
}

/** Parses the events file `hangGuardReporter.ts` writes (see `formatHangGuardEvent`). Unknown lines are ignored. */
export function readStoryProgress(text: string): { started: Set<string>; finished: Set<string> } {
  const started = new Set<string>()
  const finished = new Set<string>()
  for (const line of text.split('\n')) {
    const tab = line.indexOf('\t')
    if (tab < 0) continue
    const kind = line.slice(0, tab)
    const file = line.slice(tab + 1).trim()
    if (kind === 'start') started.add(file)
    else if (kind === 'end') finished.add(file)
  }
  return { started, finished }
}

type StoryProgress = Pick<HangGuardResult, 'inFlightStoryFiles' | 'neverStartedStoryFiles'>

function classify(expected: readonly string[], eventsText: string): StoryProgress {
  const { started, finished } = readStoryProgress(eventsText)
  return {
    inFlightStoryFiles: [...started].filter((file) => !finished.has(file)).sort(),
    neverStartedStoryFiles: expected.filter((file) => !started.has(file)),
  }
}

function listOrNone(files: readonly string[]): string {
  return files.length > 0 ? `${files.length}: ${files.join(', ')}` : '(none)'
}

export function runWithHangGuard(options: HangGuardOptions): Promise<HangGuardResult> {
  const { command, args, cwd, timeoutMs, expectedStoryFiles } = options
  const stdout = options.stdout ?? process.stdout
  const stderr = options.stderr ?? process.stderr
  const spawnFn = options.spawnFn ?? spawn
  const killFn = options.killFn ?? killProcessTree
  const invocation = `${command} ${args.join(' ')}`

  const eventsDir = mkdtempSync(join(tmpdir(), 'hang-guard-'))
  const eventsFile = join(eventsDir, 'events.log')
  writeFileSync(eventsFile, '')
  const progress = (): StoryProgress => {
    let text = ''
    try {
      text = readFileSync(eventsFile, 'utf8')
    } catch {
      // Unreadable is the same as empty: every expected file counts as never started.
    }
    return classify(expectedStoryFiles, text)
  }

  return new Promise((resolve) => {
    let settled = false
    const finish = (result: HangGuardResult): void => {
      rmSync(eventsDir, { recursive: true, force: true })
      resolve(result)
    }

    const spawnOptions: SpawnOptions = {
      cwd,
      shell: true,
      env: { ...(options.env ?? process.env), [HANG_GUARD_EVENTS_ENV]: eventsFile },
      // A detached, own-group child so `killProcessTree` can take out
      // grandchildren (esbuild, the headless browser) with one signal.
      detached: platform() !== 'win32',
      stdio: ['inherit', 'pipe', 'pipe'],
    }
    const child: ChildProcess = spawnFn(command, args, spawnOptions)

    child.stdout?.on('data', (chunk: Buffer | string) => stdout.write(chunk.toString()))
    child.stderr?.on('data', (chunk: Buffer | string) => stderr.write(chunk.toString()))

    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      const state = progress()
      const pid = child.pid
      if (pid !== undefined) killFn(pid)
      const inFlight =
        state.inFlightStoryFiles.length > 0
          ? listOrNone(state.inFlightStoryFiles)
          : '(none — every story file that started also finished; the hang is between files, or in setup/teardown)'
      stderr.write(
        `\n[hang-guard] TIMED OUT after ${timeoutMs}ms waiting for "${invocation}" to finish.\n` +
          `[hang-guard] Story file(s) that started but never finished (the likely hang): ${inFlight}\n` +
          `[hang-guard] Story file(s) that never started: ${listOrNone(state.neverStartedStoryFiles)}\n` +
          `[hang-guard] Killed the process tree (pid ${pid ?? 'unknown'}) so this fails the CI step now, ` +
          'instead of running out the ui-tests job timeout (agent-forge-harness-w1e).\n',
      )
      finish({ code: 1, timedOut: true, ...state })
    }, timeoutMs)
    // Never let this timer itself hold the process open if something else exits first.
    timer.unref?.()

    child.on('exit', (code: number | null, signal: NodeJS.Signals | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      const state = progress()
      if (code === null) {
        stderr.write(`[hang-guard] "${invocation}" was killed by signal ${signal ?? 'unknown'}; failing.\n`)
        finish({ code: 1, timedOut: false, ...state })
        return
      }
      if (code !== 0) {
        finish({ code, timedOut: false, ...state })
        return
      }
      if (expectedStoryFiles.length === 0) {
        stderr.write(`[hang-guard] "${invocation}" exited 0, but there were no story files to expect; failing.\n`)
        finish({ code: 1, timedOut: false, ...state })
        return
      }
      if (state.inFlightStoryFiles.length > 0 || state.neverStartedStoryFiles.length > 0) {
        stderr.write(
          `[hang-guard] "${invocation}" exited 0, but not every story file ran to the end; failing.\n` +
            `[hang-guard] Started but never finished: ${listOrNone(state.inFlightStoryFiles)}\n` +
            `[hang-guard] Never started: ${listOrNone(state.neverStartedStoryFiles)}\n`,
        )
        finish({ code: 1, timedOut: false, ...state })
        return
      }
      finish({ code: 0, timedOut: false, ...state })
    })

    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      stderr.write(`[hang-guard] failed to launch "${command}": ${String(error)}\n`)
      finish({ code: 1, timedOut: false, ...progress() })
    })
  })
}

const DEFAULT_TIMEOUT_MS = 480_000 // 8 minutes — the observed hang went silent for 23; a normal run finishes all 45 files in well under a minute.

export interface MainOptions {
  /** Extra command-line arguments. Defaults to `process.argv.slice(2)`; any are refused. */
  argv?: readonly string[]
  env?: NodeJS.ProcessEnv
  spawnFn?: SpawnFn
  killFn?: (pid: number) => void
  stdout?: NodeJS.WritableStream
  stderr?: NodeJS.WritableStream
}

/** Runs the storybook project under the guard from `ui/` and returns the exit code. `runStorybookTests.ts` is the CLI entry. */
export async function main(options: MainOptions = {}): Promise<number> {
  const argv = options.argv ?? process.argv.slice(2)
  const env = options.env ?? process.env
  const stderr = options.stderr ?? process.stderr
  if (argv.length > 0) {
    stderr.write(
      `[hang-guard] runStorybookTests.ts takes no arguments (got: ${argv.join(' ')}). It fails a run that skips any ` +
        'story file, so a filtered run cannot pass here; run a subset directly with: ' +
        `${STORYBOOK_COMMAND} vitest run --project=storybook <filter>\n`,
    )
    return 2
  }
  // Not `new URL('..', import.meta.url)`: under the jsdom test project the global URL is jsdom's, which fileURLToPath rejects.
  const uiDir = join(dirname(fileURLToPath(import.meta.url)), '..')
  const result = await runWithHangGuard({
    command: STORYBOOK_COMMAND,
    args: storybookVitestArgs(env),
    cwd: uiDir,
    timeoutMs: Number(env.STORYBOOK_HANG_GUARD_MS ?? DEFAULT_TIMEOUT_MS),
    expectedStoryFiles: findStoryFiles(join(uiDir, 'src')).map((file) => `src/${file}`),
    env,
    spawnFn: options.spawnFn,
    killFn: options.killFn,
    stdout: options.stdout,
    stderr,
  })
  return result.code
}
