// Checks for the Live Session Assistant threat model record (1ir.1.3), run by checkdocs.ts. They make the independent
// review's cross-reference checks permanent (PR #184: its surviving mutant and findings M-4 and M-6):
//   a. its own ids (the first cell of a table row) are defined once, and every citation of a family it defines resolves;
//   b. each threat's "Verified by" and each obligation's "Proves" name each other;
//   c. every rule is cited by some threat's controls, and every threat has a verifier and an owner;
//   d. every threat whose residual is Medium or Low-Medium is named by a residual-risk row;
//   e. every obligation has an owning bead, and the ranges the record states match the rows it has.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { REPO_ROOT } from './tracker.ts'

export const THREAT_MODEL_ADR = 'docs/adr/live-session-assistant-threat-model.md'

const ID = '[A-Z][A-Z0-9]*-\\d+'

/** Cells of a table line, split on pipes outside backticks, trimmed. */
export function cells(line: string): string[] {
  const out: string[] = []
  let cur = ''
  let code = false
  for (const ch of line.trim().replace(/^\|/, '').replace(/\|$/, '')) {
    if (ch === '`') code = !code
    if (ch === '|' && !code) { out.push(cur.trim()); cur = '' } else cur += ch
  }
  out.push(cur.trim())
  return out
}

/** Every table in the text, as rows keyed by their header cells. */
export function tables(text: string): Record<string, string>[][] {
  const lines = text.split(/\r?\n/)
  const out: Record<string, string>[][] = []
  for (let i = 0; i + 1 < lines.length; i++) {
    if (!/^\|/.test(lines[i]) || !/^\|\s*-{3}/.test(lines[i + 1]) || (i > 0 && /^\|/.test(lines[i - 1]))) continue
    const head = cells(lines[i])
    const rows: Record<string, string>[] = []
    for (let j = i + 2; j < lines.length && /^\|/.test(lines[j]); j++) {
      const c = cells(lines[j])
      rows.push(Object.fromEntries(head.map((h, k) => [h, c[k] ?? ''])))
    }
    out.push(rows)
  }
  return out
}

/** Every id of `family` a clause names, with `X-3 to X-5` expanded (zero-padded as the range's first id is). */
export function idsIn(clause: string, family: string): Set<string> {
  const out = new Set<string>()
  for (const m of clause.matchAll(new RegExp(`(?<![A-Za-z0-9-])${family}-(\\d+)(?: to ${family}-(\\d+))?`, 'g'))) {
    const width = m[1].length
    const hi = m[2] ? Number(m[2]) : Number(m[1])
    for (let n = Number(m[1]); n <= hi; n++) out.add(`${family}-${String(n).padStart(width, '0')}`)
  }
  return out
}

export function checkThreatModel(text: string): string[] {
  const problems = new Set<string>()
  const p = (s: string) => problems.add(`${THREAT_MODEL_ADR}: ${s}`)
  const all = tables(text).flat()
  const first = (r: Record<string, string>) => Object.values(r)[0] ?? ''
  const rowsOf = (family: string) => all.filter((r) => new RegExp(`^${family}-\\d+$`).test(first(r)))

  // a. Ids are defined once, and every citation of a family the record defines resolves.
  const defined = new Set<string>()
  for (const r of all) {
    const id = first(r)
    if (!new RegExp(`^${ID}$`).test(id)) continue
    if (defined.has(id)) p(`${id} is defined twice`)
    defined.add(id)
  }
  const families = [...new Set([...defined].map((id) => id.replace(/-\d+$/, '')))].sort((a, b) => b.length - a.length)
  if (!families.length) p('defines no ids')
  else for (const m of text.matchAll(new RegExp(`(?<![A-Za-z0-9-])((?:${families.join('|')})-\\d+)\\b`, 'g'))) {
    if (!defined.has(m[1])) p(`cites ${m[1]}, which it does not define`)
  }

  const tm = rowsOf('TM').filter((r) => 'Verified by' in r)
  const lv = rowsOf('LV').filter((r) => 'Proves' in r)
  if (!tm.length || !lv.length) p('no threat table or no verification table')
  const live = tm.filter((r) => !/^\*\*Superseded/.test(r.Threat ?? ''))

  // b. Verified by and Proves are mutual.
  const verifiedBy = new Map(tm.map((r) => [first(r), idsIn(r['Verified by'], 'LV')]))
  const proves = new Map(lv.map((r) => [first(r), idsIn(r.Proves, 'TM')]))
  for (const [t, vs] of verifiedBy) for (const v of vs) if (!proves.get(v)?.has(t)) p(`${t} is verified by ${v}, whose Proves omits it`)
  for (const [v, ts] of proves) for (const t of ts) if (!verifiedBy.get(t)?.has(v)) p(`${v} proves ${t}, whose Verified by omits it`)

  // c. Every rule is some threat's control; every threat has a verifier and an owner.
  const controlled = new Set(live.flatMap((r) => [...idsIn(r.Controls ?? '', 'LS')]))
  for (const r of rowsOf('LS')) if (!controlled.has(first(r))) p(`${first(r)} is cited by no threat's controls`)
  for (const r of live) {
    if (!/\bLV-\d+|\bT-\d+/.test(r['Verified by'])) p(`${first(r)} has no verifier`)
    if (!/`[^`]+`/.test(r.Owners ?? '')) p(`${first(r)} has no owning bead`)
  }

  // d. A Medium or Low-Medium residual is named by a residual-risk row, so the owner's acceptance covers it.
  const named = new Set(rowsOf('RR').flatMap((r) => [...idsIn(r.Threats ?? '', 'TM')]))
  for (const r of live) if (/Medium/.test(r.Residual ?? '') && !named.has(first(r))) p(`${first(r)}'s residual is ${r.Residual.split(/[ :(]/)[0]} and no RR row names it`)

  // e. Obligations are owned, and the stated ranges match the rows.
  for (const r of lv) if (!/`[^`]+`/.test(r.Owners ?? '')) p(`${first(r)} has no owning bead`)
  for (const family of ['LV', 'RR', 'LQ', 'CQ']) {
    const n = rowsOf(family).length
    if (n && !text.includes(`${family}-1 to ${family}-${n}`)) p(`no statement names the range ${family}-1 to ${family}-${n}`)
  }
  return [...problems]
}

export function checkThreatModelFile(root: string): string[] {
  return checkThreatModel(readFileSync(join(root, THREAT_MODEL_ADR), 'utf8'))
}

// Run alone (CI does: it needs no tracker and no other document), after a self-test, so that a checker gone blind
// fails instead of passing: the review's surviving mutant, a threat citing a verification row that does not exist.
if (import.meta.main) {
  const text = readFileSync(join(REPO_ROOT, THREAT_MODEL_ADR), 'utf8')
  const mutant = text.replace(/^(\| TM-\d+ \|.*?)LV-\d+/m, '$1LV-999')
  if (idsIn('LS-23 to LS-25; LV-4', 'LS').size !== 3 || mutant === text || !checkThreatModel(mutant).length) {
    process.stdout.write('self-test failed: the checker no longer sees a threat citing an undefined LV row\n')
    process.exit(1)
  }
  const problems = checkThreatModel(text)
  for (const s of problems) process.stdout.write(` - ${s}\n`)
  process.stdout.write(problems.length ? `${problems.length} problem(s)\n` : `OK: ${THREAT_MODEL_ADR} has no problems\n`)
  process.exit(problems.length ? 1 : 0)
}
