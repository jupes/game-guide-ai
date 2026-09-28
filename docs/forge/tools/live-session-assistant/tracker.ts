// Shared helpers for the Live Session Assistant plan tooling: repository paths, argument parsing, and the Beads
// tracker export.
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

/** Repository root: this folder is docs/forge/tools/live-session-assistant. */
export const REPO_ROOT = resolve(import.meta.dir, '..', '..', '..', '..')
export const PREFIX = 'agent-forge-harness-'
export const DELIVERY_DOC = join(REPO_ROOT, 'docs', 'forge', 'plans', 'live-session-assistant-delivery.md')

export interface TrackerIssue {
  id: string
  title: string
  status: string
  priority?: number
  issue_type?: string
  labels?: string[]
  description?: string
  acceptance_criteria?: string
  design?: string
  spec_id?: string
  dependencies?: { issue_id?: string; depends_on_id: string; type: string }[]
}

/** Value that follows `name` on the command line, if present. */
export function argValue(name: string): string | undefined {
  const i = process.argv.indexOf(name)
  return i >= 0 ? process.argv[i + 1] : undefined
}

export const hasFlag = (name: string) => process.argv.includes(name)

/**
 * Directory to run `bd` in: the repository root unless `--tracker-cwd` is given. From inside the repository `bd`
 * resolves this repository's `.beads` (git worktrees resolve to the main checkout's); from outside it, such as the
 * harness root, it finds a different tracker, which `loadTracker` rejects.
 */
export const trackerCwd = () => resolve(argValue('--tracker-cwd') ?? REPO_ROOT)

/** Run `bd` with the given arguments in the tracker directory. */
export function bd(argv: string[]) {
  const p = Bun.spawnSync(['bd', ...argv], { cwd: trackerCwd(), stdout: 'pipe', stderr: 'pipe' })
  return { code: p.exitCode, out: p.stdout.toString(), err: p.stderr.toString() }
}

/**
 * Tracker issues from `--snapshot <file>` (a `bd list --all --json --limit 0` export) or, by default, from `bd`.
 * Refuses a tracker that lacks the game-guide-ai epics, which catches running against the wrong `.beads` folder.
 * With `optional`, returns undefined when no tracker is available instead of throwing.
 */
export function loadTracker(optional = false): TrackerIssue[] | undefined {
  let issues: TrackerIssue[] | undefined
  const snapshot = argValue('--snapshot')
  if (snapshot) {
    issues = JSON.parse(readFileSync(resolve(snapshot), 'utf8'))
  } else {
    try {
      const r = bd(['list', '--all', '--json', '--limit', '0'])
      if (r.code === 0) issues = JSON.parse(r.out)
      else if (!optional) throw new Error(`bd list failed: ${r.err.trim().slice(0, 300)}`)
    } catch (e) {
      if (!optional) throw e
    }
  }
  if (issues && !issues.some((i) => i.id === `${PREFIX}1kg`)) {
    throw new Error('this tracker has no agent-forge-harness-1kg epic; pass --tracker-cwd <game-guide-ai checkout> or --snapshot <export>')
  }
  return issues
}

/** Created bead IDs by plan key, recorded when the plan was materialized. */
export const IDS_PATH = join(import.meta.dir, 'ids.json')
export const ids: Record<string, string> = existsSync(IDS_PATH) ? JSON.parse(readFileSync(IDS_PATH, 'utf8')) : {}
