// Validates the Live Session Assistant bead specification against the Beads tracker before (or after) creation.
// Rules R1-R11: hierarchy and numbering, references, cycles over the combined blocking graph (plan edges plus every
// existing tracker edge), text quality, gate shape and chaining, phase monotonicity, per-phase gate coverage, and
// player-disclosure ordering.
//
// Usage: bun validate.ts [--snapshot <bd list --all --json export>] [--tracker-cwd <dir>] [--out <summary.json>]
// The run recorded in the delivery plan used the pre-creation export (152 issues, 308 blocks edges).
import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { BEADS, type Bead } from './spec.ts'
import { PREFIX, argValue, loadTracker, type TrackerIssue } from './tracker.ts'

const extAll: TrackerIssue[] = loadTracker()!
const extById = new Map(extAll.map((i) => [i.id, i]))
const extBlocksEdges = extAll.reduce((n, i) => n + (i.dependencies ?? []).filter((d) => d.type === 'blocks').length, 0)

const errors: string[] = []
const warnings: string[] = []
const err = (m: string) => errors.push(m)
const extId = (r: string) => PREFIX + r.slice(4)
const node = (r: string) => (r.startsWith('ext:') ? extId(r) : `L:${r}`)

// ── R1 keys, parents, hierarchy, contiguous child numbering
const byKey = new Map<string, Bead>()
for (const b of BEADS) {
  if (byKey.has(b.key)) err(`R1 duplicate key ${b.key}`)
  byKey.set(b.key, b)
}
const children = new Map<string, Bead[]>()
for (const b of BEADS) {
  if (b.key === 'EPIC') {
    if (b.type !== 'epic' || b.parent) err('R1 EPIC must be an epic without parent')
    continue
  }
  if (!b.parent || !byKey.has(b.parent)) { err(`R1 ${b.key} has missing parent ${b.parent}`); continue }
  const p = byKey.get(b.parent)!
  const expectedParentKey = b.key.includes('.') ? b.key.slice(0, b.key.lastIndexOf('.')) : 'EPIC'
  if (b.parent !== expectedParentKey) err(`R1 ${b.key} parent ${b.parent} does not match key path (${expectedParentKey})`)
  if (p.type === 'epic' && b.type !== 'feature') err(`R1 ${b.key} under epic must be a feature`)
  if (p.type === 'feature' && !['task', 'decision'].includes(b.type)) err(`R1 ${b.key} under feature must be task/decision`)
  if (['task', 'decision'].includes(p.type) && !['task', 'decision'].includes(b.type)) err(`R1 ${b.key} sub-task type invalid`)
  children.set(b.parent, [...(children.get(b.parent) ?? []), b])
}
const isLeaf = (b: Bead) => !(children.get(b.key)?.length)
for (const [parent, kids] of children) {
  const nums = kids.map((k) => Number(k.key.split('.').at(-1))).sort((a, b) => a - b)
  nums.forEach((n, i) => { if (n !== i + 1) err(`R1 non-contiguous child keys under ${parent}: ${nums.join(',')}`) })
}

// ── R2 references
const checkRef = (from: string, ref: string, kind: string) => {
  if (ref.startsWith('ext:')) { if (!extById.has(extId(ref))) err(`R2 ${from} ${kind} unknown external ${ref}`); return }
  if (!byKey.has(ref)) err(`R2 ${from} ${kind} unknown local key ${ref}`)
  if (ref === from) err(`R2 ${from} ${kind} references itself`)
}
for (const b of BEADS) {
  for (const r of b.blockedBy ?? []) checkRef(b.key, r, 'blockedBy')
  for (const r of b.relatesTo ?? []) checkRef(b.key, r, 'relatesTo')
  for (const r of b.blocks ?? []) { if (!r.startsWith('ext:')) err(`R2 ${b.key} blocks must reference external beads`); checkRef(b.key, r, 'blocks') }
  const overlap = (b.blockedBy ?? []).filter((r) => (b.relatesTo ?? []).includes(r) || (b.blocks ?? []).includes(r))
  if (overlap.length) err(`R2 ${b.key} has conflicting edge types to ${overlap.join(',')}`)
}

// ── R3 cycles over the combined blocking graph (plan edges + edges into existing beads + existing tracker edges)
const adj = new Map<string, string[]>() // node -> nodes it depends on
const addEdge = (a: string, c: string) => adj.set(a, [...(adj.get(a) ?? []), c])
let localEdges = 0, extOutEdges = 0, extInEdges = 0
for (const b of BEADS) {
  for (const r of b.blockedBy ?? []) { addEdge(`L:${b.key}`, node(r)); if (r.startsWith('ext:')) extOutEdges++; else localEdges++ }
  for (const r of b.blocks ?? []) { addEdge(extId(r), `L:${b.key}`); extInEdges++ }
}
for (const i of extAll) for (const d of i.dependencies ?? []) if (d.type === 'blocks') addEdge(i.id, d.depends_on_id)
const color = new Map<string, number>()
const stack: string[] = []
const cycles: string[][] = []
const dfs = (n: string) => {
  color.set(n, 1); stack.push(n)
  for (const m of adj.get(n) ?? []) {
    const c = color.get(m) ?? 0
    if (c === 1) cycles.push([...stack.slice(stack.indexOf(m)), m])
    else if (c === 0) dfs(m)
  }
  stack.pop(); color.set(n, 2)
}
for (const n of adj.keys()) if ((color.get(n) ?? 0) === 0) dfs(n)
for (const c of cycles) err(`R3 cycle: ${c.join(' -> ')}`)

const closureCache = new Map<string, Set<string>>()
const closure = (n: string): Set<string> => {
  if (closureCache.has(n)) return closureCache.get(n)!
  const out = new Set<string>()
  closureCache.set(n, out)
  for (const m of adj.get(n) ?? []) { out.add(m); for (const x of closure(m)) out.add(x) }
  return out
}

// ── R4 text quality, R11 titles and LSA references
for (const b of BEADS) {
  if (!b.acceptance || b.acceptance.trim().length < 60) err(`R4 ${b.key} acceptance criteria missing or too short`)
  if (!b.description || b.description.trim().length < 40) err(`R4 ${b.key} description missing or too short`)
  const title = `[game-guide-ai] ${b.title}`
  if (title.length > 120) err(`R11 ${b.key} title too long (${title.length})`)
  if (/\.$/.test(b.title)) err(`R11 ${b.key} title ends with period`)
  for (const m of `${b.description} ${b.design ?? ''} ${b.acceptance}`.matchAll(/LSA-(\d+(?:\.\d+)*)/g)) {
    if (!byKey.has(m[1])) err(`R11 ${b.key} text references unknown LSA-${m[1]}`)
  }
}

// ── R5 priorities and gate shape
for (const b of BEADS) {
  if (![0, 1, 2, 3].includes(b.priority)) err(`R5 ${b.key} invalid priority`)
  if (b.gate !== undefined) {
    if (b.priority !== 0) err(`R5 gate ${b.key} must be P0`)
    if (!(b.labels ?? []).includes('security-gate')) err(`R5 gate ${b.key} missing security-gate label`)
    if (!isLeaf(b)) err(`R5 gate ${b.key} must be a leaf`)
    if (b.phase !== b.gate) err(`R5 gate ${b.key} phase ${b.phase} != gate ${b.gate}`)
  }
}

// ── R6 phase monotonicity for local blockers
for (const b of BEADS) for (const r of b.blockedBy ?? []) {
  if (r.startsWith('ext:')) continue
  const o = byKey.get(r)
  if (!o || b.phase === null || o.phase === null) continue
  if (o.phase > b.phase) err(`R6 ${b.key} (phase ${b.phase}) blocked by later-phase ${r} (phase ${o.phase})`)
}

// ── R7 gates 0..5 exist once and chain
const gates = BEADS.filter((b) => b.gate !== undefined)
const gateOf = (p: number) => gates.find((g) => g.gate === p)
for (let p = 0; p <= 5; p++) {
  const g = gates.filter((b) => b.gate === p)
  if (g.length !== 1) { err(`R7 phase ${p} has ${g.length} gates`); continue }
  if (p >= 1) {
    const prev = gateOf(p - 1)
    if (prev && !closure(`L:${g[0].key}`).has(`L:${prev.key}`)) err(`R7 gate ${g[0].key} does not depend on previous gate ${prev.key}`)
  }
}

// ── R8b every non-exempt leaf of phase N is required by gate N; post-gate enablement beads follow gate N
const requiredBy = (p: number) => { const g = gateOf(p); return g ? closure(`L:${g.key}`) : new Set<string>() }
for (const b of BEADS) {
  if (!isLeaf(b) || b.gate !== undefined || b.gateExempt) continue
  if (b.phase === null) { err(`R8 ${b.key} leaf without phase`); continue }
  if (b.postGate !== undefined) {
    if (b.postGate !== b.phase) err(`R8 ${b.key} postGate ${b.postGate} != phase ${b.phase}`)
    if (!closure(`L:${b.key}`).has(`L:${gateOf(b.postGate)!.key}`)) err(`R8 ${b.key} post-gate enablement does not follow gate ${b.postGate}`)
    continue
  }
  const parent = b.parent ? byKey.get(b.parent) : undefined
  const inOwn = requiredBy(b.phase).has(`L:${b.key}`) || (parent && parent.type !== 'feature' && requiredBy(b.phase).has(`L:${parent.key}`))
  if (!inOwn) err(`R8b ${b.key} (phase ${b.phase}) is not required by its own phase gate`)
}

// ── R9 player disclosure safety ordering
const mustDepend = (b: Bead, target: string, why: string) => {
  if (!closure(`L:${b.key}`).has(`L:${target}`)) err(`R9 ${b.key} must transitively depend on ${target} (${why})`)
}
for (const b of BEADS) {
  const labels = b.labels ?? []
  if (labels.includes('player-disclosure-enable')) {
    mustDepend(b, '13.5', 'Phase 4 security gate')
    mustDepend(b, '13.6', 'adversarial suite')
    mustDepend(b, '1.2', 'eligibility model')
    if ((b.phase ?? 0) < 4) err(`R9 ${b.key} player disclosure before Phase 4`)
  }
  if (labels.includes('player-path-build') && b.type !== 'feature') mustDepend(b, '1.2', 'eligibility model')
}

// ── R10 features have children
for (const b of BEADS) if (b.type === 'feature' && !children.get(b.key)?.length) err(`R10 feature ${b.key} has no children`)

// ── report
const count = (f: (b: Bead) => string) => BEADS.reduce<Record<string, number>>((a, b) => { const k = f(b); a[k] = (a[k] ?? 0) + 1; return a }, {})
const relEdges = BEADS.reduce((n, b) => n + (b.relatesTo?.length ?? 0), 0)
const ready = BEADS.filter((b) => isLeaf(b) && !(b.blockedBy ?? []).some((r) => !r.startsWith('ext:') || extById.get(extId(r))?.status !== 'closed'))
const g1 = gateOf(1)!
const directG1ext = new Set<string>()
for (const n of [`L:${g1.key}`, ...closure(`L:${g1.key}`)]) {
  if (!n.startsWith('L:')) continue
  for (const r of byKey.get(n.slice(2))?.blockedBy ?? []) if (r.startsWith('ext:')) directG1ext.add(extId(r))
}
const summary = {
  trackerIssues: extAll.length,
  trackerBlocksEdges: extBlocksEdges,
  beads: BEADS.length,
  byType: count((b) => b.type),
  byPriority: count((b) => `P${b.priority}`),
  byPhase: count((b) => String(b.phase)),
  leaves: BEADS.filter(isLeaf).length,
  gates: gates.map((g) => `${g.key}=G${g.gate}`),
  internalBlockEdges: localEdges,
  edgesToExistingBeads: extOutEdges,
  edgesFromExistingBeads: extInEdges,
  relatesToEdges: relEdges,
  firstReadyLeaves: ready.map((b) => `${b.key} [P${b.priority}] ${b.title}`),
  mvpDirectExternalBlockers: [...directG1ext].map((i) => `${i.replace(PREFIX, '')} (${extById.get(i)?.status})`),
  errors: errors.length,
  warnings: warnings.length,
}
const out = argValue('--out')
if (out) writeFileSync(resolve(out), JSON.stringify(summary, null, 2))
console.log(JSON.stringify(summary, null, 2))
if (warnings.length) console.log('\nWARNINGS:\n  ' + warnings.join('\n  '))
if (errors.length) { console.log('\nERRORS (' + errors.length + '):\n  ' + errors.join('\n  ')); process.exit(1) }
console.log('\nVALID: no errors')
