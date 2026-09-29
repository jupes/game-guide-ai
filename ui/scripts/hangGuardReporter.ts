/**
 * hangGuardReporter (agent-forge-harness-w1e) — a vitest reporter that
 * records when each test module starts and finishes, so
 * `scripts/hangGuard.ts` can tell a story file that is in flight from one
 * that never started, and a finished file from one whose path merely shows
 * up in a console line.
 *
 * It runs inside the vitest (Node) process, not the browser tab, and appends
 * one line per event to the file named by the `HANG_GUARD_EVENTS_FILE`
 * environment variable:
 *
 *     start<TAB>src/gm/GameDocument.stories.tsx
 *     end<TAB>src/gm/GameDocument.stories.tsx
 *
 * Paths are `TestModule.relativeModuleId` (relative to the project root, which
 * is `ui/`), with forward slashes. The writes are synchronous, so everything
 * recorded before a hang or a kill is already on disk when the guard reads it.
 * Without the environment variable it records nothing, so loading it by hand
 * is harmless.
 */

import { appendFileSync } from 'node:fs'
import type { Reporter, TestModule } from 'vitest/node'

export const HANG_GUARD_EVENTS_ENV = 'HANG_GUARD_EVENTS_FILE'

export type HangGuardEventKind = 'start' | 'end'

/** One events-file line. `hangGuard.ts`'s `readStoryProgress` is the parser. */
export function formatHangGuardEvent(kind: HangGuardEventKind, file: string): string {
  return `${kind}\t${file.split('\\').join('/')}\n`
}

export interface HangGuardReporterOptions {
  /** Where to append events. Defaults to `process.env.HANG_GUARD_EVENTS_FILE`. */
  eventsFile?: string
}

export default class HangGuardReporter implements Reporter {
  private readonly eventsFile: string | undefined

  // vitest constructs a CLI-named reporter with its options object (`{}`), so
  // this takes an options object too, never a bare path.
  constructor(options: HangGuardReporterOptions = {}) {
    const file = options.eventsFile ?? process.env[HANG_GUARD_EVENTS_ENV]
    this.eventsFile = file === undefined || file === '' ? undefined : file
  }

  onTestModuleStart(testModule: TestModule): void {
    this.record('start', testModule)
  }

  onTestModuleEnd(testModule: TestModule): void {
    this.record('end', testModule)
  }

  private record(kind: HangGuardEventKind, testModule: TestModule): void {
    if (this.eventsFile === undefined) return
    appendFileSync(this.eventsFile, formatHangGuardEvent(kind, testModule.relativeModuleId))
  }
}
