// Regenerates the two generated regions of the delivery plan from spec.ts, ids.json, and the tracker:
//   - the Beads hierarchy between the BEADS-TABLES markers (section 6);
//   - the cross-initiative table in section 5.1 (titles and statuses come from the tracker).
//
// Usage: bun tables.ts --check | --write [--snapshot <bd export>] [--tracker-cwd <dir>]
// --check exits 1 when a region is out of date. Without a tracker, only the Beads hierarchy is handled.
import { readFileSync, writeFileSync } from 'node:fs'
import { BEADS, type Bead } from './spec.ts'
import { DELIVERY_DOC, PREFIX, hasFlag, ids, loadTracker } from './tracker.ts'

const check = hasFlag('--check')
if (check === hasFlag('--write')) { console.error('usage: bun tables.ts --check | --write'); process.exit(2) }

const short = (id: string) => id.replace(PREFIX, '')
const idOf = (key: string) => (ids[key] ? `\`${short(ids[key])}\`` : `\`LSA-${key}\``)
const ref = (r: string) => (r.startsWith('ext:') ? `\`${r.slice(4)}\`` : idOf(r))
const esc = (s: string) => s.replace(/\|/g, '\\|')
const byKey = new Map(BEADS.map((b) => [b.key, b]))
const childrenOf = (key: string) => BEADS.filter((b) => b.parent === key)
const typeLabel = (b: Bead) => (b.gate !== undefined ? `**gate G${b.gate}**` : b.type)

function beadsTables(): string {
  const lines: string[] = []
  const epic = byKey.get('EPIC')!
  lines.push('| Epic | Priority | Title |', '| --- | --- | --- |', `| ${idOf('EPIC')} | P${epic.priority} | ${esc(epic.title)} |`, '')
  lines.push(`Epic relates to: ${(epic.relatesTo ?? []).map(ref).join(', ')}`, '')
  for (const f of childrenOf('EPIC')) {
    lines.push(`### ${idOf(f.key)} — ${esc(f.title)} (P${f.priority})`, '')
    lines.push(f.description, '')
    lines.push(`**Feature acceptance:** ${f.acceptance}`, '')
    lines.push('| Bead | Type | P | Phase | Title | Blocked by | Relates to |', '| --- | --- | --- | --- | --- | --- | --- |')
    const walk = (parentKey: string, depth: number) => {
      for (const c of childrenOf(parentKey)) {
        const indent = depth > 0 ? '↳ ' : ''
        lines.push(`| ${indent}${idOf(c.key)} | ${typeLabel(c)} | P${c.priority} | ${c.phase ?? '—'} | ${esc(c.title)} | ${(c.blockedBy ?? []).map(ref).join(', ') || '—'} | ${(c.relatesTo ?? []).map(ref).join(', ') || '—'} |`)
        walk(c.key, depth + 1)
      }
    }
    walk(f.key, 0)
    const outward: string[] = []
    const collect = (parentKey: string) => {
      for (const c of childrenOf(parentKey)) {
        if (c.blocks?.length) outward.push(`${idOf(c.key)} blocks ${c.blocks.map(ref).join(', ')}`)
        collect(c.key)
      }
    }
    collect(f.key)
    if (outward.length) lines.push('', `**Blocks beads in other initiatives:** ${outward.join('; ')}.`)
    lines.push('', '<details><summary>Acceptance criteria</summary>', '')
    const walkAc = (parentKey: string) => {
      for (const c of childrenOf(parentKey)) {
        lines.push(`- **${idOf(c.key)} ${esc(c.title)}** — ${c.acceptance}${c.gateExempt ? ` _(Gate-exempt: ${c.gateExempt})_` : ''}`)
        walkAc(c.key)
      }
    }
    walkAc(f.key)
    lines.push('', '</details>', '')
  }
  return lines.join('\n').trimEnd()
}

function crossInitiativeTable(tracker: { id: string; title: string; status: string }[]): string[] {
  const byId = new Map(tracker.map((i) => [short(i.id), i]))
  const local = (key: string) => (ids[key] ? `\`${short(ids[key])}\`` : `\`LSA-${key}\``)
  const rows = new Map<string, { blocksOurs: string[]; blockedByOurs: string[]; relates: string[] }>()
  const row = (ext: string) => {
    if (!rows.has(ext)) rows.set(ext, { blocksOurs: [], blockedByOurs: [], relates: [] })
    return rows.get(ext)!
  }
  for (const b of BEADS) {
    for (const r of b.blockedBy ?? []) if (r.startsWith('ext:')) row(r.slice(4)).blocksOurs.push(b.key)
    for (const r of b.blocks ?? []) if (r.startsWith('ext:')) row(r.slice(4)).blockedByOurs.push(b.key)
    for (const r of b.relatesTo ?? []) if (r.startsWith('ext:')) row(r.slice(4)).relates.push(b.key)
  }
  const naturalKey = (s: string) => s.split('.').map((p) => p.padStart(4, '0')).join('.')
  const list = (keys: string[]) => (keys.length ? keys.map(local).join(', ') : '—')
  const title = (s: string) => esc(s).replace(/^\[game-guide-ai\]\s*/, '')
  const lines = [
    '| External bead | Title | Status | Blocks our beads | Blocked by our beads | Relates to |',
    '| --- | --- | --- | --- | --- | --- |',
  ]
  for (const ext of [...rows.keys()].sort((a, b) => naturalKey(a).localeCompare(naturalKey(b)))) {
    const issue = byId.get(ext)
    const r = rows.get(ext)!
    lines.push(`| \`${ext}\` | ${issue ? title(issue.title) : '**missing**'} | ${issue?.status ?? '—'} | ${list(r.blocksOurs)} | ${list(r.blockedByOurs)} | ${list(r.relates)} |`)
  }
  return lines
}

const original = readFileSync(DELIVERY_DOC, 'utf8')
let doc = original
const stale: string[] = []

// Beads hierarchy between the markers
const start = '<!-- BEADS-TABLES:START -->'
const end = '<!-- BEADS-TABLES:END -->'
const i = doc.indexOf(start)
const j = doc.indexOf(end)
if (i < 0 || j < i) throw new Error('BEADS-TABLES markers not found')
const withTables = doc.slice(0, i + start.length) + '\n\n' + beadsTables() + '\n\n' + doc.slice(j)
if (withTables !== doc) stale.push('Beads hierarchy (section 6)')
doc = withTables

// Cross-initiative table in section 5.1
const tracker = loadTracker(true)
if (!tracker) {
  console.log('no tracker available: skipped the cross-initiative table')
} else {
  const lines = doc.split('\n')
  const s = lines.findIndex((l) => l.startsWith('| External bead | Title |'))
  if (s < 0) throw new Error('cross-initiative table not found')
  let e = s
  while (e < lines.length && lines[e].startsWith('|')) e++
  const table = crossInitiativeTable(tracker)
  if (lines.slice(s, e).join('\n') !== table.join('\n')) stale.push('cross-initiative table (section 5.1)')
  lines.splice(s, e - s, ...table)
  doc = lines.join('\n')
}

if (check) {
  if (stale.length) { console.log(`out of date: ${stale.join('; ')} (run bun tables.ts --write)`); process.exit(1) }
  console.log('generated tables are up to date')
} else {
  if (doc !== original) writeFileSync(DELIVERY_DOC, doc)
  console.log(stale.length ? `updated: ${stale.join('; ')}` : 'generated tables were already up to date')
}
