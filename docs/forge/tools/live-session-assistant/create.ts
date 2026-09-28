// Materializes the validated Live Session Assistant bead graph with bd. This already ran on 2026-09-17
// (epic agent-forge-harness-1ir); it is kept as the record of how the beads and edges were created.
//
// Usage:
//   bun create.ts --dry-run [--tracker-cwd <dir>]   check every external reference against the tracker; writes nothing
//   bun create.ts --really [--resume]               create beads and edges; refuses while ids.json exists unless --resume
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { BASE_LABELS, BEADS, SPEC_ID, type Bead } from './spec.ts'
import { IDS_PATH, PREFIX, loadTracker, trackerCwd } from './tracker.ts'

const REPO = trackerCwd()
const idsPath = IDS_PATH
const logPath = join(import.meta.dir, 'create-log.txt')
const args = new Set(process.argv.slice(2))
if (args.has('--dry-run')) {
  // Print what would be created and linked, and check every external reference against the live tracker. Writes nothing.
  const live = new Set(loadTracker()!.map((i) => i.id))
  const extRefs = new Set<string>()
  let blockedBy = 0, blocks = 0, relates = 0
  for (const b of BEADS) {
    for (const r of b.blockedBy ?? []) { blockedBy++; if (r.startsWith('ext:')) extRefs.add(PREFIX + r.slice(4)) }
    for (const r of b.blocks ?? []) { blocks++; extRefs.add(PREFIX + r.slice(4)) }
    for (const r of b.relatesTo ?? []) { relates++; if (r.startsWith('ext:')) extRefs.add(PREFIX + r.slice(4)) }
  }
  const missing = [...extRefs].filter((id) => !live.has(id))
  const typeCounts = BEADS.reduce<Record<string, number>>((a, b) => { a[b.type] = (a[b.type] ?? 0) + 1; return a }, {})
  console.log(JSON.stringify({ trackerIssues: live.size, creates: BEADS.length, typeCounts, blockedByEdges: blockedBy, blocksEdgesIntoExisting: blocks, relatesEdges: relates, externalRefs: extRefs.size, missingExternalRefs: missing }, null, 2))
  process.exit(missing.length ? 1 : 0)
}
if (!args.has('--really')) { console.error('refusing: pass --really'); process.exit(2) }
const resume = args.has('--resume')
if (existsSync(idsPath) && !resume) { console.error('refusing: ids.json exists (use --resume)'); process.exit(2) }

const ids: Record<string, string> = existsSync(idsPath) ? JSON.parse(readFileSync(idsPath, 'utf8')) : {}
const log: string[] = []
const bd = (argv: string[]) => {
  const p = Bun.spawnSync(['bd', ...argv], { cwd: REPO, stdout: 'pipe', stderr: 'pipe' })
  const out = p.stdout.toString(), err = p.stderr.toString()
  log.push(`$ bd ${argv.map((a) => (a.length > 60 ? a.slice(0, 57) + '...' : a)).join(' ')}\n  exit=${p.exitCode}\n  out=${out.trim().slice(0, 300)}\n  err=${err.trim().split('\n').filter((l) => !/beads.role|git config beads.role/.test(l)).join(' | ').slice(0, 300)}`)
  return { code: p.exitCode, out, err }
}
const flush = () => { writeFileSync(idsPath, JSON.stringify(ids, null, 2)); writeFileSync(logPath, log.join('\n')) }

// natural key order: EPIC first, then numeric segments
const keyCmp = (a: string, b: string) => {
  if (a === 'EPIC') return -1
  if (b === 'EPIC') return 1
  const x = a.split('.').map(Number), y = b.split('.').map(Number)
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if (x[i] === undefined) return -1
    if (y[i] === undefined) return 1
    if (x[i] !== y[i]) return x[i] - y[i]
  }
  return 0
}
const ordered = [...BEADS].sort((a, b) => keyCmp(a.key, b.key))
const extId = (r: string) => PREFIX + r.slice(4)

// Predicted hierarchical ids: epicId + '.' + key (keys are contiguous per parent, validated below)
const predict = (epicId: string, key: string) => (key === 'EPIC' ? epicId : `${epicId}.${key}`)
for (const parent of new Set(BEADS.map((b) => b.parent).filter(Boolean) as string[])) {
  const kids = BEADS.filter((b) => b.parent === parent).map((b) => Number(b.key.split('.').at(-1))).sort((a, b) => a - b)
  kids.forEach((n, i) => { if (n !== i + 1) throw new Error(`non-contiguous child keys under ${parent}: ${kids}`) })
}

const substitute = (text: string, epicId: string) => text.replace(/LSA-(\d+(?:\.\d+)*)/g, (_, k: string) => {
  if (!BEADS.some((b) => b.key === k)) throw new Error(`unknown LSA ref ${k}`)
  return predict(epicId, k)
})

// ── 1. create beads in order
for (const b of ordered) {
  if (ids[b.key]) continue
  const epicId = ids.EPIC
  if (b.key !== 'EPIC' && !epicId) throw new Error('epic id missing')
  const argv = [
    'create', '--silent', '--no-inherit-labels',
    '--type', b.type, '--priority', String(b.priority),
    '--title', `[game-guide-ai] ${b.title}`,
    '--labels', [...BASE_LABELS, ...(b.labels ?? [])].join(','),
    '--spec-id', SPEC_ID,
  ]
  const sub = (t: string) => (epicId ? substitute(t, epicId) : t)
  argv.push('--description', sub(b.description), '--acceptance', sub(b.acceptance))
  if (b.design) argv.push('--design', sub(b.design))
  if (b.parent) argv.push('--parent', ids[b.parent])
  const r = bd(argv)
  const id = r.out.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.startsWith(PREFIX)).at(-1)
  if (r.code !== 0 || !id) { flush(); throw new Error(`create failed for ${b.key}: ${r.err || r.out}`) }
  if (epicId && id !== predict(epicId, b.key)) { ids[b.key] = id; flush(); throw new Error(`id mismatch for ${b.key}: got ${id}, expected ${predict(epicId, b.key)}`) }
  ids[b.key] = id
  flush()
  console.log('created', b.key, id)
}

// ── 2. dependencies (blocks) and relates-to links
const want: { from: string; to: string; type: 'blocks' | 'relates-to' }[] = []
for (const b of ordered) {
  for (const r of b.blockedBy ?? []) want.push({ from: ids[b.key], to: r.startsWith('ext:') ? extId(r) : ids[r], type: 'blocks' })
  // `blocks` lists existing beads in other initiatives that must wait for this bead: the external bead is the blocked side.
  for (const r of b.blocks ?? []) want.push({ from: extId(r), to: ids[b.key], type: 'blocks' })
  for (const r of b.relatesTo ?? []) want.push({ from: ids[b.key], to: r.startsWith('ext:') ? extId(r) : ids[r], type: 'relates-to' })
}
for (const e of want) if (!e.from || !e.to) { flush(); throw new Error(`unresolved edge endpoint: ${JSON.stringify(e)}`) }
const existing = () => {
  const r = bd(['list', '--all', '--json', '--limit', '0'])
  const all = JSON.parse(r.out) as { id: string; dependencies?: { depends_on_id: string; type: string }[] }[]
  const set = new Set<string>()
  for (const i of all) for (const d of i.dependencies ?? []) set.add(`${i.id}|${d.depends_on_id}|${d.type}`)
  return set
}
let have = existing()
for (const e of want) {
  if (have.has(`${e.from}|${e.to}|${e.type}`)) continue
  const r = e.type === 'blocks' ? bd(['dep', 'add', e.from, e.to]) : bd(['dep', 'add', e.from, e.to, '--type', 'relates-to'])
  if (r.code !== 0) { flush(); throw new Error(`dep add failed ${e.from} -> ${e.to} (${e.type}): ${r.err || r.out}`) }
}
flush()

// ── 3. verify every intended edge exists (bd can fail silently)
have = existing()
const missing = want.filter((e) => !have.has(`${e.from}|${e.to}|${e.type}`))
const cycles = bd(['dep', 'cycles'])
const report = {
  created: Object.keys(ids).length,
  expected: BEADS.length,
  edgesIntended: want.length,
  edgesMissing: missing,
  cyclesOutput: (cycles.out + cycles.err).split('\n').filter((l) => l && !/beads.role|git config beads.role/.test(l)).join(' | '),
}
writeFileSync(join(import.meta.dir, 'create-report.json'), JSON.stringify(report, null, 2))
flush()
console.log(JSON.stringify(report, null, 2))
if (missing.length || report.created !== report.expected) process.exit(1)
