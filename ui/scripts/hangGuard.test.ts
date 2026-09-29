/**
 * hangGuard.test — proves the external watchdog fires on a stuck child, names
 * the story file that started and never finished, refuses a "clean" exit that
 * skipped story files, and stays out of the way of a normal run
 * (agent-forge-harness-w1e).
 *
 * These use a fake `spawnFn` (a bare `EventEmitter` standing in for a
 * `ChildProcess`) rather than a real subprocess: the behaviour under test is
 * the *external* timer-and-kill logic, not whether `vitest`/Chromium
 * actually hang, which a real browser can't be made to do to order in a unit
 * test. The fake child plays the part of `scripts/hangGuardReporter.ts` by
 * appending `start`/`end` lines to the events file the guard hands it in the
 * spawn environment — the same line format that reporter writes (pinned on
 * the reporter side by `hangGuardReporter.test.ts`).
 *
 * The last test runs the real CLI entry, `scripts/runStorybookTests.ts`,
 * under Bun with a 1ms deadline, so an entry point that never reaches
 * `main()` cannot pass silently.
 */

import { describe, expect, it, vi } from 'vitest'
import { EventEmitter } from 'node:events'
import { spawn, spawnSync, type SpawnOptions } from 'node:child_process'
import { appendFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  HANG_GUARD_EVENTS_ENV,
  STORYBOOK_COMMAND,
  findStoryFiles,
  killProcessTree,
  main,
  runWithHangGuard,
  storybookVitestArgs,
} from './hangGuard'

const UI_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')

interface SpawnCall {
  command: string
  args: readonly string[]
  options: SpawnOptions
}

/** A `ChildProcess`-shaped fake: stdout/stderr emitters plus exit/error on the process itself. Never calls `.kill()` — the guard is expected to use the injected `killFn` for process-tree teardown, not the child object's own method. */
function fakeSpawn(pid = 4242) {
  const stdout = new EventEmitter()
  const stderr = new EventEmitter()
  const child = Object.assign(new EventEmitter(), { pid, stdout, stderr })
  const calls: SpawnCall[] = []
  const spawnFn = vi.fn((command: string, args: readonly string[], options: SpawnOptions) => {
    calls.push({ command, args, options })
    return child as never
  })
  /** What `hangGuardReporter.ts` does from inside the vitest process: one tab-separated line per module start/end. */
  const record = (kind: 'start' | 'end', file: string): void => {
    const eventsFile = calls[0]?.options.env?.[HANG_GUARD_EVENTS_ENV]
    if (eventsFile === undefined) throw new Error('the guard did not hand the child an events file in its environment')
    appendFileSync(eventsFile, `${kind}\t${file}\n`)
  }
  const ran = (file: string): void => {
    record('start', file)
    record('end', file)
  }
  return {
    child,
    spawnFn,
    calls,
    record,
    ran,
    exit: (code: number | null, signal: NodeJS.Signals | null = null) => child.emit('exit', code, signal),
  }
}

function sink(): { lines: string[]; stream: NodeJS.WritableStream } {
  const lines: string[] = []
  return { lines, stream: { write: (s: string) => void lines.push(s) } as never }
}

const GAME_DOCUMENT = 'src/gm/GameDocument.stories.tsx'
const DOCUMENT_FIELD = 'src/gm/DocumentField.stories.tsx'
const CHAT_PANE = 'src/shell/ChatPane.stories.tsx'

describe('runWithHangGuard', () => {
  it('resolves cleanly and never kills anything when every story file starts and finishes before the deadline', async () => {
    const fake = fakeSpawn()
    const killFn = vi.fn()
    const out = sink()
    const err = sink()

    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run', '--project=storybook'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [GAME_DOCUMENT],
      spawnFn: fake.spawnFn,
      killFn,
      stdout: out.stream,
      stderr: err.stream,
    })

    fake.ran(GAME_DOCUMENT)
    fake.child.stdout.emit('data', ' ✓ src/gm/GameDocument.stories.tsx (31 tests) 3570ms\n')
    fake.exit(0)

    const result = await pending
    expect(result).toEqual({ code: 0, timedOut: false, inFlightStoryFiles: [], neverStartedStoryFiles: [] })
    expect(killFn).not.toHaveBeenCalled()
    expect(out.lines.join('')).toContain('GameDocument.stories.tsx')
    expect(err.lines).toEqual([])
  })

  it("tees the child's stderr to our stderr, not our stdout", async () => {
    const fake = fakeSpawn()
    const out = sink()
    const err = sink()
    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [GAME_DOCUMENT],
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stdout: out.stream,
      stderr: err.stream,
    })
    fake.child.stderr.emit('data', 'stderr | src/gm/GameDocument.stories.tsx > Stat Block\n')
    fake.ran(GAME_DOCUMENT)
    fake.exit(0)
    await pending
    expect(err.lines.join('')).toContain('stderr | src/gm/GameDocument.stories.tsx > Stat Block')
    expect(out.lines.join('')).not.toContain('Stat Block')
  })

  it('propagates a non-zero exit code from a normal (non-hung) failure without treating it as a hang', async () => {
    const fake = fakeSpawn()
    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run', '--project=storybook'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [GAME_DOCUMENT],
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stderr: sink().stream,
    })
    fake.ran(GAME_DOCUMENT)
    fake.exit(1)
    await expect(pending).resolves.toEqual({ code: 1, timedOut: false, inFlightStoryFiles: [], neverStartedStoryFiles: [] })
  })

  it('fails when the child is killed by a signal (exit code null), e.g. an OOM SIGKILL', async () => {
    const fake = fakeSpawn()
    const err = sink()
    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [GAME_DOCUMENT],
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stderr: err.stream,
    })
    fake.ran(GAME_DOCUMENT)
    fake.exit(null, 'SIGKILL')
    const result = await pending
    expect(result.code).toBe(1)
    expect(result.timedOut).toBe(false)
    expect(err.lines.join('')).toContain('SIGKILL')
  })

  it('fails when the child cannot be launched at all', async () => {
    const fake = fakeSpawn()
    const err = sink()
    const pending = runWithHangGuard({
      command: 'bunx',
      args: ['vitest'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [GAME_DOCUMENT],
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stderr: err.stream,
    })
    fake.child.emit('error', new Error('spawn bunx ENOENT'))
    const result = await pending
    expect(result.code).toBe(1)
    expect(result.timedOut).toBe(false)
    expect(err.lines.join('')).toContain('spawn bunx ENOENT')
  })

  it('fails a zero exit when an expected story file never ran, and names it (agent-forge-harness-w1e H1)', async () => {
    const fake = fakeSpawn()
    const err = sink()
    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run', '--project=storybook'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [DOCUMENT_FIELD, GAME_DOCUMENT],
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stderr: err.stream,
    })
    fake.ran(DOCUMENT_FIELD)
    fake.exit(0)
    const result = await pending
    expect(result).toEqual({ code: 1, timedOut: false, inFlightStoryFiles: [], neverStartedStoryFiles: [GAME_DOCUMENT] })
    expect(err.lines.join('')).toContain(GAME_DOCUMENT)
    expect(err.lines.join('')).not.toContain(DOCUMENT_FIELD)
  })

  it('fails a zero exit that ran no story files at all (the whole storybook run emptied)', async () => {
    const fake = fakeSpawn()
    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run', '--project=jsdom'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [DOCUMENT_FIELD, GAME_DOCUMENT],
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stderr: sink().stream,
    })
    fake.exit(0)
    const result = await pending
    expect(result.code).toBe(1)
    expect(result.neverStartedStoryFiles).toEqual([DOCUMENT_FIELD, GAME_DOCUMENT])
  })

  it('fails a zero exit when it was given no story files to expect', async () => {
    const fake = fakeSpawn()
    const err = sink()
    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run', '--project=storybook'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [],
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stderr: err.stream,
    })
    fake.exit(0)
    const result = await pending
    expect(result.code).toBe(1)
    expect(err.lines.join('')).toContain('no story files')
  })

  it('on the deadline, kills the tree and names the file that started and never finished, not a file that only logged (H2)', async () => {
    vi.useFakeTimers()
    try {
      const fake = fakeSpawn(9001)
      const killFn = vi.fn()
      const err = sink()

      const pending = runWithHangGuard({
        command: 'bunx',
        args: ['vitest', 'run', '--project=storybook'],
        cwd: '/repo/ui',
        timeoutMs: 5000,
        expectedStoryFiles: [CHAT_PANE, DOCUMENT_FIELD, GAME_DOCUMENT],
        spawnFn: fake.spawnFn,
        killFn,
        stdout: sink().stream,
        stderr: err.stream,
      })

      fake.ran(DOCUMENT_FIELD)
      fake.child.stdout.emit('data', ' ✓ src/gm/DocumentField.stories.tsx (12 tests) 900ms\n')
      // ChatPane starts, logs a console line naming its own path (CI run
      // 36377294380 printed exactly this shape), then never finishes. A path
      // substring in the output is not a result.
      fake.record('start', CHAT_PANE)
      fake.child.stderr.emit('data', 'stderr | src/shell/ChatPane.stories.tsx > Sent By Keyboard\n')
      // GameDocument never starts.

      // The child never exits: this is the hang. Advance past the deadline.
      await vi.advanceTimersByTimeAsync(5001)

      const result = await pending
      expect(result).toEqual({
        code: 1,
        timedOut: true,
        inFlightStoryFiles: [CHAT_PANE],
        neverStartedStoryFiles: [GAME_DOCUMENT],
      })
      expect(killFn).toHaveBeenCalledWith(9001)
      expect(killFn).toHaveBeenCalledTimes(1)
      const report = err.lines.join('')
      expect(report).toContain('TIMED OUT')
      expect(report).toMatch(/started but never finished[^\n]*src\/shell\/ChatPane\.stories\.tsx/)
      expect(report).toMatch(/never started[^\n]*src\/gm\/GameDocument\.stories\.tsx/)
      expect(report).not.toMatch(/started but never finished[^\n]*DocumentField/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not fire the kill twice when the child happens to exit right at the deadline', async () => {
    vi.useFakeTimers()
    try {
      const fake = fakeSpawn()
      const killFn = vi.fn()
      const pending = runWithHangGuard({
        command: 'vitest',
        args: ['run'],
        cwd: '/repo/ui',
        timeoutMs: 1000,
        expectedStoryFiles: [GAME_DOCUMENT],
        spawnFn: fake.spawnFn,
        killFn,
      })
      fake.ran(GAME_DOCUMENT)
      fake.exit(0)
      await vi.advanceTimersByTimeAsync(1001)
      const result = await pending
      expect(result.timedOut).toBe(false)
      expect(killFn).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('the storybook invocation (pinned: agent-forge-harness-b4v, agent-forge-harness-w1e H1)', () => {
  it('runs exactly the storybook project through bunx vitest, with the hang-guard reporter beside the default one', () => {
    expect(STORYBOOK_COMMAND).toBe('bunx')
    expect(storybookVitestArgs({})).toEqual([
      'vitest',
      'run',
      '--project=storybook',
      '--reporter=default',
      '--reporter=./scripts/hangGuardReporter.ts',
    ])
  })

  it('keeps the github-actions reporter vitest would otherwise add by itself on CI (naming --reporter drops it)', () => {
    expect(storybookVitestArgs({ GITHUB_ACTIONS: 'true' })).toEqual([
      'vitest',
      'run',
      '--project=storybook',
      '--reporter=default',
      '--reporter=github-actions',
      '--reporter=./scripts/hangGuardReporter.ts',
    ])
  })
})

describe('main', () => {
  it('spawns the pinned invocation in ui/, expecting every story file under src/, and passes on a complete run', async () => {
    const fake = fakeSpawn()
    const storyFiles = findStoryFiles(join(UI_DIR, 'src')).map((file) => `src/${file}`)
    expect(storyFiles.length).toBeGreaterThan(0)

    const pending = main({ argv: [], env: {}, spawnFn: fake.spawnFn, killFn: vi.fn(), stdout: sink().stream, stderr: sink().stream })
    expect(fake.calls).toHaveLength(1)
    const [call] = fake.calls
    expect(call.command).toBe('bunx')
    expect(call.args).toEqual(storybookVitestArgs({}))
    expect(join(String(call.options.cwd))).toBe(join(UI_DIR))
    for (const file of storyFiles) fake.ran(file)
    fake.exit(0)
    await expect(pending).resolves.toBe(0)
  })

  it('builds the args from, and hands the child, the environment it was given (so CI keeps its github-actions reporter)', async () => {
    const fake = fakeSpawn()
    const pending = main({
      argv: [],
      env: { GITHUB_ACTIONS: 'true', HANG_GUARD_TEST_MARKER: 'carried' },
      spawnFn: fake.spawnFn,
      killFn: vi.fn(),
      stdout: sink().stream,
      stderr: sink().stream,
    })
    const [call] = fake.calls
    expect(call.args).toEqual(storybookVitestArgs({ GITHUB_ACTIONS: 'true' }))
    expect(call.args).toContain('--reporter=github-actions')
    expect(call.options.env?.HANG_GUARD_TEST_MARKER).toBe('carried')
    fake.exit(1)
    await expect(pending).resolves.toBe(1)
  })

  it('fails when the run skipped one of the story files under src/', async () => {
    const fake = fakeSpawn()
    const storyFiles = findStoryFiles(join(UI_DIR, 'src')).map((file) => `src/${file}`)
    const pending = main({ argv: [], env: {}, spawnFn: fake.spawnFn, killFn: vi.fn(), stdout: sink().stream, stderr: sink().stream })
    for (const file of storyFiles.slice(1)) fake.ran(file)
    fake.exit(0)
    await expect(pending).resolves.toBe(1)
  })

  it('refuses extra arguments instead of silently running a filtered (and then "incomplete") storybook project', async () => {
    const fake = fakeSpawn()
    const err = sink()
    const code = await main({ argv: ['GameDocument'], env: {}, spawnFn: fake.spawnFn, killFn: vi.fn(), stderr: err.stream })
    expect(code).toBe(2)
    expect(fake.spawnFn).not.toHaveBeenCalled()
    expect(err.lines.join('')).toContain('bunx vitest run --project=storybook')
  })
})

describe('runStorybookTests.ts (the CLI entry point)', () => {
  it('actually runs the guard: with a 1ms deadline it times out, names the storybook command and exits 1', () => {
    const run = spawnSync('bun', ['scripts/runStorybookTests.ts'], {
      cwd: UI_DIR,
      env: { ...process.env, STORYBOOK_HANG_GUARD_MS: '1' },
      encoding: 'utf8',
      timeout: 60_000,
    })
    expect(run.error).toBeUndefined()
    expect(run.stderr).toContain('[hang-guard] TIMED OUT after 1ms')
    expect(run.stderr).toContain('bunx vitest run --project=storybook')
    expect(run.status).toBe(1)
  }, 90_000)
})

describe('findStoryFiles', () => {
  it('finds every *.stories.tsx file under a directory, ignoring node_modules and dotfiles, sorted', () => {
    const root = mkdtempSync(join(tmpdir(), 'hang-guard-test-'))
    try {
      mkdirSync(join(root, 'gm'), { recursive: true })
      mkdirSync(join(root, 'node_modules', 'somepkg'), { recursive: true })
      mkdirSync(join(root, '.hidden'), { recursive: true })
      writeFileSync(join(root, 'gm', 'GameDocument.stories.tsx'), '')
      writeFileSync(join(root, 'gm', 'DocumentField.stories.tsx'), '')
      writeFileSync(join(root, 'gm', 'GameDocument.test.tsx'), '')
      writeFileSync(join(root, 'node_modules', 'somepkg', 'Ignored.stories.tsx'), '')
      writeFileSync(join(root, '.hidden', 'AlsoIgnored.stories.tsx'), '')

      expect(findStoryFiles(root)).toEqual(['gm/DocumentField.stories.tsx', 'gm/GameDocument.stories.tsx'])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

describe('killProcessTree (the real OS-level kill; every runWithHangGuard test above injects a fake killFn, so ' +
  'this function itself -- including its own catch/fallback branches -- was never once invoked; agent-forge-harness-8ug)', () => {
  it('actually terminates a real, running process', async () => {
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'])
    const pid = child.pid
    if (pid === undefined) throw new Error('failed to spawn a test child process')
    const exited = new Promise<void>((resolve) => child.on('exit', () => resolve()))

    killProcessTree(pid)

    await exited // hangs out to the test's own timeout if the kill did nothing
  }, 10_000)

  it('does not throw when the process has already exited (the best-effort fallback swallows the error)', async () => {
    const child = spawn(process.execPath, ['-e', 'process.exit(0)'])
    const pid = child.pid
    if (pid === undefined) throw new Error('failed to spawn a test child process')
    await new Promise<void>((resolve) => child.on('exit', () => resolve()))

    // On win32 this exercises the catch around `taskkill` failing to find a
    // dead pid; on POSIX it exercises both `process.kill(-pid, ...)` and the
    // `process.kill(pid, ...)` fallback beneath it, since a pid that no
    // longer exists satisfies neither.
    expect(() => killProcessTree(pid)).not.toThrow()
  }, 10_000)
})
