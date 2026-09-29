/**
 * hangGuard (agent-forge-harness-w1e) — a wall-clock supervisor for the
 * Storybook browser test project, so a hung story fails the CI step directly
 * instead of riding out to the `ui-tests` job's 25-minute timeout.
 *
 * ## Why Vitest's own `testTimeout` cannot be trusted here
 *
 * Vitest wraps every test's handler in `withTimeout`: a `Promise.race`
 * against a `setTimeout` (see `@vitest/runner`'s `chunk-artifact.js`). For the
 * `storybook` project that timer runs *inside the browser tab* — the test
 * file, `@storybook/addon-vitest`'s `testStory` wrapper and the timeout race
 * itself are all bundled into the page and executed there; Node only
 * receives the result over the browser bridge once the page reports it.
 *
 * That means a tab whose JS thread is wedged — a synchronous loop, or a
 * feedback loop between a `selectionchange` listener and the render it
 * triggers — never gets to run its own timeout timer either. Nor does a tab
 * that has simply lost its CDP connection. Either way Node just waits
 * forever for a message that is never coming, because the mechanism that
 * would send it is the thing that is stuck.
 *
 * That is exactly what CI run 36377294380 showed (bead agent-forge-harness-
 * w1e): the storybook project reported 44 of its 45 story files, then
 * `GameDocument.stories.tsx` went silent for the rest of the job — no
 * "Test timed out" error, just silence until the 25-minute job cap cancelled
 * it. A `testTimeout` value only fires from inside the same event loop that
 * hung, so raising or lowering it would not have changed that outcome.
 *
 * ## What this does instead
 *
 * It runs the storybook `vitest` invocation as a child process, tees its
 * output to our own stdout/stderr so CI logs read exactly as before on a
 * normal run, and races the whole run against an external, Node-side timer.
 * If that timer fires, it force-kills the *process tree* — which does not
 * need the browser tab's cooperation, unlike an in-page timeout — and prints
 * which of the known story files never reported a result, by checking
 * whether that file's path appears anywhere in the captured output. That
 * survives a reporter format change; parsing the exact checkmark line would
 * not.
 */

import { spawn, execFileSync, type ChildProcess, type SpawnOptions } from 'node:child_process'
import { readdirSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { platform } from 'node:os'
import { fileURLToPath } from 'node:url'

export interface HangGuardResult {
  /** The process exit code to propagate (0 on a clean pass). */
  code: number
  timedOut: boolean
  /** Story files (relative, forward-slash paths) that never appeared in the captured output. Only meaningful when `timedOut`. */
  missingStoryFiles: string[]
}

export interface HangGuardOptions {
  command: string
  args: string[]
  cwd: string
  timeoutMs: number
  /** Every story file the run was expected to report on, relative to `cwd`, forward-slash separated. */
  expectedStoryFiles: readonly string[]
  /** Test seam: defaults to the real `child_process.spawn`. */
  spawnFn?: typeof spawn
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

export function runWithHangGuard(options: HangGuardOptions): Promise<HangGuardResult> {
  const { command, args, cwd, timeoutMs, expectedStoryFiles } = options
  const stdout = options.stdout ?? process.stdout
  const stderr = options.stderr ?? process.stderr
  const spawnFn = options.spawnFn ?? spawn
  const killFn = options.killFn ?? killProcessTree

  return new Promise((resolve) => {
    let settled = false
    let captured = ''

    const spawnOptions: SpawnOptions = {
      cwd,
      shell: true,
      // A detached, own-group child so `killProcessTree` can take out
      // grandchildren (esbuild, the headless browser) with one signal.
      detached: platform() !== 'win32',
      stdio: ['inherit', 'pipe', 'pipe'],
    }
    const child: ChildProcess = spawnFn(command, args, spawnOptions)

    const tee = (chunk: Buffer | string, sink: NodeJS.WritableStream): void => {
      const text = chunk.toString()
      captured += text
      sink.write(text)
    }
    child.stdout?.on('data', (chunk: Buffer | string) => tee(chunk, stdout))
    child.stderr?.on('data', (chunk: Buffer | string) => tee(chunk, stderr))

    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      const missing = expectedStoryFiles.filter((file) => !captured.includes(file))
      const pid = child.pid
      if (pid !== undefined) killFn(pid)
      const named =
        missing.length > 0
          ? missing.join(', ')
          : '(none — every known story file already appeared in the output; the hang may be in teardown or a file added after this list was built)'
      stderr.write(
        `\n[hang-guard] TIMED OUT after ${timeoutMs}ms waiting for "${command} ${args.join(' ')}" to finish.\n` +
          `[hang-guard] Story file(s) that never reported a result: ${named}\n` +
          `[hang-guard] Killed the process tree (pid ${pid ?? 'unknown'}) so this fails the CI step now, ` +
          'instead of running out the ui-tests job timeout (agent-forge-harness-w1e).\n',
      )
      resolve({ code: 1, timedOut: true, missingStoryFiles: missing })
    }, timeoutMs)
    // Never let this timer itself hold the process open if something else exits first.
    timer.unref?.()

    child.on('exit', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve({ code: code ?? 1, timedOut: false, missingStoryFiles: [] })
    })

    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      stderr.write(`[hang-guard] failed to launch "${command}": ${String(error)}\n`)
      resolve({ code: 1, timedOut: false, missingStoryFiles: [] })
    })
  })
}

const DEFAULT_TIMEOUT_MS = 480_000 // 8 minutes — the observed hang went silent for 23; a normal run reports 44/45 files in well under a minute.

async function main(): Promise<void> {
  const uiDir = fileURLToPath(new URL('..', import.meta.url))
  const timeoutMs = Number(process.env.STORYBOOK_HANG_GUARD_MS ?? DEFAULT_TIMEOUT_MS)
  const expectedStoryFiles = findStoryFiles(join(uiDir, 'src'))
  const result = await runWithHangGuard({
    command: 'bunx',
    args: ['vitest', 'run', '--project=storybook', ...process.argv.slice(2)],
    cwd: uiDir,
    timeoutMs,
    expectedStoryFiles: expectedStoryFiles.map((file) => join('src', file).split(sep).join('/')),
  })
  process.exit(result.code)
}

// Runs only when this file is the process entry point, not when a test imports it.
if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1]) {
  void main()
}
