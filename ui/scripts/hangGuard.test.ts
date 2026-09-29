/**
 * hangGuard.test — proves the external watchdog fires on a stuck child, names
 * the story that never reported, and stays out of the way of a clean run
 * (agent-forge-harness-w1e).
 *
 * These use a fake `spawnFn` (a bare `EventEmitter` standing in for a
 * `ChildProcess`) rather than a real subprocess: the behaviour under test is
 * the *external* timer-and-kill logic, not whether `vitest`/Chromium
 * actually hang, which is exactly what a real browser can't be made to do to
 * order in a unit test.
 */

import { describe, expect, it, vi } from 'vitest'
import { EventEmitter } from 'node:events'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findStoryFiles, runWithHangGuard } from './hangGuard'

/** A `ChildProcess`-shaped fake: stdout/stderr emitters plus exit/error on the process itself. Never calls `.kill()` — the guard is expected to use the injected `killFn` for process-tree teardown, not the child object's own method. */
function fakeChild(pid = 4242): {
  child: EventEmitter & { pid: number; stdout: EventEmitter; stderr: EventEmitter }
  finish: (code: number) => void
} {
  const stdout = new EventEmitter()
  const stderr = new EventEmitter()
  const child = Object.assign(new EventEmitter(), { pid, stdout, stderr })
  return {
    child,
    finish: (code: number) => child.emit('exit', code),
  }
}

describe('runWithHangGuard', () => {
  it('resolves cleanly and never kills anything when the child exits before the deadline', async () => {
    const { child, finish } = fakeChild()
    const spawnFn = vi.fn(() => child as never)
    const killFn = vi.fn()
    const stdout: string[] = []

    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run', '--project=storybook'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: ['src/gm/GameDocument.stories.tsx'],
      spawnFn,
      killFn,
      stdout: { write: (s: string) => void stdout.push(s) } as never,
      stderr: { write: (s: string) => void stdout.push(s) } as never,
    })

    child.stdout.emit('data', ' ✓ src/gm/GameDocument.stories.tsx (31 tests) 3570ms\n')
    finish(0)

    const result = await pending
    expect(result).toEqual({ code: 0, timedOut: false, missingStoryFiles: [] })
    expect(killFn).not.toHaveBeenCalled()
    expect(stdout.join('')).toContain('GameDocument.stories.tsx')
  })

  it('propagates a non-zero exit code from a normal (non-hung) failure without treating it as a hang', async () => {
    const { child, finish } = fakeChild()
    const pending = runWithHangGuard({
      command: 'vitest',
      args: ['run', '--project=storybook'],
      cwd: '/repo/ui',
      timeoutMs: 1000,
      expectedStoryFiles: [],
      spawnFn: () => child as never,
      killFn: vi.fn(),
    })
    finish(1)
    await expect(pending).resolves.toEqual({ code: 1, timedOut: false, missingStoryFiles: [] })
  })

  it('kills the process tree and names the story that never reported once the deadline passes', async () => {
    vi.useFakeTimers()
    try {
      const { child } = fakeChild(9001)
      const killFn = vi.fn()
      const stderrLines: string[] = []

      const pending = runWithHangGuard({
        command: 'bunx',
        args: ['vitest', 'run', '--project=storybook'],
        cwd: '/repo/ui',
        timeoutMs: 5000,
        expectedStoryFiles: ['src/gm/GameDocument.stories.tsx', 'src/gm/DocumentField.stories.tsx'],
        spawnFn: () => child as never,
        killFn,
        stdout: { write: () => {} } as never,
        stderr: { write: (s: string) => void stderrLines.push(s) } as never,
      })

      // 44 of 45 files reported, matching the CI evidence — everything except
      // GameDocument.stories.tsx shows up in the captured output.
      child.stdout.emit('data', ' ✓ src/gm/DocumentField.stories.tsx (12 tests) 900ms\n')

      // The child never exits: this is the hang. Advance past the deadline.
      await vi.advanceTimersByTimeAsync(5001)

      const result = await pending
      expect(result.timedOut).toBe(true)
      expect(result.code).toBe(1)
      expect(result.missingStoryFiles).toEqual(['src/gm/GameDocument.stories.tsx'])
      expect(killFn).toHaveBeenCalledWith(9001)
      expect(killFn).toHaveBeenCalledTimes(1)
      expect(stderrLines.join('')).toContain('src/gm/GameDocument.stories.tsx')
      expect(stderrLines.join('')).toContain('TIMED OUT')
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not fire the kill twice when the child happens to exit right at the deadline', async () => {
    vi.useFakeTimers()
    try {
      const { child, finish } = fakeChild()
      const killFn = vi.fn()
      const pending = runWithHangGuard({
        command: 'vitest',
        args: ['run'],
        cwd: '/repo/ui',
        timeoutMs: 1000,
        expectedStoryFiles: [],
        spawnFn: () => child as never,
        killFn,
      })
      finish(0)
      await vi.advanceTimersByTimeAsync(1001)
      const result = await pending
      expect(result.timedOut).toBe(false)
      expect(killFn).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
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
