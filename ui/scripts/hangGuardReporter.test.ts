/**
 * hangGuardReporter.test — pins the events-file contract between the vitest
 * reporter (writer) and `hangGuard.ts` (reader), agent-forge-harness-w1e.
 */

import { describe, expect, it } from 'vitest'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import HangGuardReporter, { HANG_GUARD_EVENTS_ENV, formatHangGuardEvent } from './hangGuardReporter'
import { readStoryProgress } from './hangGuard'

function withEventsFile(run: (file: string) => void): void {
  const dir = mkdtempSync(join(tmpdir(), 'hang-guard-reporter-test-'))
  try {
    const file = join(dir, 'events.log')
    writeFileSync(file, '')
    run(file)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

const moduleAt = (relativeModuleId: string) => ({ relativeModuleId }) as never

describe('HangGuardReporter', () => {
  it('appends one tab-separated start and end line per test module, with forward-slash paths', () => {
    withEventsFile((file) => {
      const reporter = new HangGuardReporter({ eventsFile: file })
      reporter.onTestModuleStart(moduleAt('src/gm/GameDocument.stories.tsx'))
      reporter.onTestModuleStart(moduleAt('src\\shell\\ChatPane.stories.tsx'))
      reporter.onTestModuleEnd(moduleAt('src/gm/GameDocument.stories.tsx'))

      expect(readFileSync(file, 'utf8')).toBe(
        'start\tsrc/gm/GameDocument.stories.tsx\n' +
          'start\tsrc/shell/ChatPane.stories.tsx\n' +
          'end\tsrc/gm/GameDocument.stories.tsx\n',
      )
    })
  })

  it('writes what hangGuard.ts reads: started, finished and in-flight files round-trip', () => {
    withEventsFile((file) => {
      const reporter = new HangGuardReporter({ eventsFile: file })
      reporter.onTestModuleStart(moduleAt('src/gm/GameDocument.stories.tsx'))
      reporter.onTestModuleStart(moduleAt('src/shell/ChatPane.stories.tsx'))
      reporter.onTestModuleEnd(moduleAt('src/gm/GameDocument.stories.tsx'))

      const progress = readStoryProgress(readFileSync(file, 'utf8'))
      expect([...progress.started].sort()).toEqual(['src/gm/GameDocument.stories.tsx', 'src/shell/ChatPane.stories.tsx'])
      expect([...progress.finished]).toEqual(['src/gm/GameDocument.stories.tsx'])
    })
  })

  it('reads the events file from HANG_GUARD_EVENTS_FILE when vitest constructs it with an empty options object', () => {
    withEventsFile((file) => {
      const previous = process.env[HANG_GUARD_EVENTS_ENV]
      process.env[HANG_GUARD_EVENTS_ENV] = file
      try {
        new HangGuardReporter({}).onTestModuleStart(moduleAt('src/gm/GameDocument.stories.tsx'))
      } finally {
        if (previous === undefined) delete process.env[HANG_GUARD_EVENTS_ENV]
        else process.env[HANG_GUARD_EVENTS_ENV] = previous
      }
      expect(readFileSync(file, 'utf8')).toBe(formatHangGuardEvent('start', 'src/gm/GameDocument.stories.tsx'))
    })
  })

  it('records nothing, and does not throw, without an events file', () => {
    const previous = process.env[HANG_GUARD_EVENTS_ENV]
    delete process.env[HANG_GUARD_EVENTS_ENV]
    try {
      expect(() => new HangGuardReporter().onTestModuleEnd(moduleAt('src/gm/GameDocument.stories.tsx'))).not.toThrow()
    } finally {
      if (previous !== undefined) process.env[HANG_GUARD_EVENTS_ENV] = previous
    }
  })
})
