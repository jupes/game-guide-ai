// Compares the created beads in the tracker with spec.ts and reports drift: status, priority, type, title prefix,
// labels, spec_id, parent edge, acceptance criteria, unsubstituted LSA tokens, and the Workbench beads that must stay
// blocked by the eligibility decision. After implementation starts, status and text drift is expected; read the
// output as a report, not a failure.
//
// Usage: bun verify-created.ts [--snapshot <bd export>] [--tracker-cwd <dir>]
import { BEADS } from './spec.ts'
import { PREFIX, bd, hasFlag, ids, loadTracker } from './tracker.ts'

const all = loadTracker()!
const byId = new Map(all.map((i) => [i.id, i]))
const problems: string[] = []
const leafKeys = new Set(BEADS.filter((b) => !BEADS.some((c) => c.parent === b.key)).map((b) => b.key))
for (const b of BEADS) {
  const i = byId.get(ids[b.key])
  if (!i) { problems.push(`missing ${b.key}`); continue }
  if (i.status !== 'open') problems.push(`${b.key} status ${i.status}`)
  if (i.priority !== b.priority) problems.push(`${b.key} priority ${i.priority} != ${b.priority}`)
  if (i.issue_type !== b.type) problems.push(`${b.key} type ${i.issue_type} != ${b.type}`)
  if (!i.title.startsWith('[game-guide-ai] ')) problems.push(`${b.key} title prefix`)
  const labels = i.labels ?? []
  for (const l of ['aetheril', 'game-guide-ai', 'live-session', ...(b.labels ?? [])]) if (!labels.includes(l)) problems.push(`${b.key} missing label ${l}`)
  const ac = i.acceptance_criteria ?? ''
  if (leafKeys.has(b.key) && ac.trim().length < 60) problems.push(`${b.key} acceptance missing`)
  if (/LSA-\d/.test([i.description, ac, i.design].filter(Boolean).join(' '))) problems.push(`${b.key} unsubstituted LSA token`)
  if (i.spec_id !== 'docs/forge/plans/live-session-assistant.md') problems.push(`${b.key} spec_id ${i.spec_id}`)
  if (b.parent) {
    const pc = (i.dependencies ?? []).find((d) => d.type === 'parent-child')
    if (!pc || pc.depends_on_id !== ids[b.parent]) problems.push(`${b.key} parent-child edge wrong (${pc?.depends_on_id})`)
  }
}
for (const b of BEADS) {
  for (const r of b.blocks ?? []) {
    const ext = byId.get(PREFIX + r.slice(4))
    if (!(ext?.dependencies ?? []).some((d) => d.type === 'blocks' && d.depends_on_id === ids[b.key])) problems.push(`${r.slice(4)} not blocked by ${b.key}`)
  }
}

let readyLeaves: string[] | undefined
if (!hasFlag('--snapshot')) {
  const r = bd(['ready', '--json', '--limit', '0'])
  if (r.code === 0) {
    readyLeaves = (JSON.parse(r.out) as { id: string }[])
      .map((i) => i.id)
      .filter((id) => id.startsWith(`${ids.EPIC}.`))
      .map((id) => id.slice(ids.EPIC.length + 1))
      .filter((key) => leafKeys.has(key))
      .sort()
  }
}
console.log(JSON.stringify({ trackerIssues: all.length, planBeads: all.filter((i) => i.id === ids.EPIC || i.id.startsWith(`${ids.EPIC}.`)).length, problems, readyLeaves }, null, 2))
if (problems.length) process.exit(1)
