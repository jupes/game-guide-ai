// Mechanical consistency checks over the Live Session Assistant plan documents (read-only):
//   1. every LSA-<key> reference resolves to a key in spec.ts;
//   2. backticked bead IDs (1ir, 1kg, xiu, yje, ...) exist in the tracker or ids.json (skipped without a tracker);
//   3. markdown tables have consistent column counts;
//   4. backticked repository paths exist in the working tree or on origin/master;
//   5. code fences are balanced;
//   6. threat IDs cited in the threat model are defined in its catalog.
//
// Usage: bun checkdocs.ts [--snapshot <bd export>] [--tracker-cwd <dir>]
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { BEADS } from './spec.ts'
import { PREFIX, REPO_ROOT, ids, loadTracker } from './tracker.ts'

const DOCS = [
  'docs/forge/research/live-session-assistant-gap-analysis.md',
  'docs/forge/plans/live-session-assistant.md',
  'docs/forge/research/live-session-assistant-threat-model.md',
  'docs/forge/research/live-session-assistant-cost-model.md',
  'docs/forge/plans/live-session-assistant-delivery.md',
  'docs/forge/reports/live-session-assistant-plan-review.md',
  'docs/forge/reports/live-session-assistant-plan-review-2.md',
  // Not a plan document: the account identity record (yje.1.6), whose answer table the billing machines consume, so
  // its tables, cited paths and bead IDs get the same mechanical checks. Appended so DOCS[2] stays the threat model.
  'docs/adr/account-identity-state-machine.md',
]
// Paths cited on purpose that are not on master yet: the design archive (never in the repo), the Workbench decision
// record and plans (PR #58) and wire contract (PR #59), and the billing and retrieval plans (committed separately).
const PENDING_ELSEWHERE = [
  'docs/wire-schema.md',
  'docs/INTEGRATION.md',
  'docs/adr/gm-workbench-interactions.md',
  'docs/workbench-wire-contract.md',
  'docs/forge/plans/aetheril-gm-workbench-expansion.md',
  'docs/forge/research/aetheril-gm-workbench-gap-analysis.md',
  'docs/forge/plans/subscription-billing-coupons-profitability.md',
  'docs/forge/plans/additive-retrieval-web-fallback.md',
  'docs/forge/reports/additive-retrieval-and-billing-plan-review.md',
]

const keys = new Set(BEADS.map((b) => b.key))
const tracker = loadTracker(true)
const trackerIds = new Set([...(tracker ?? []).map((i) => i.id), ...Object.values(ids)].map((id) => id.replace(PREFIX, '')))

const problems: string[] = []
const stats: Record<string, number> = {}
for (const rel of DOCS) {
  const path = join(REPO_ROOT, rel)
  if (!existsSync(path)) { problems.push(`missing doc ${rel}`); continue }
  const text = readFileSync(path, 'utf8')
  const lines = text.split(/\r?\n/)

  // 1. Review reports may cite historical keys (for example LSA-D01 or 11.7) that later revisions removed.
  let lsaRefs = 0
  for (const m of text.matchAll(/LSA-((?:\d+)(?:\.\d+)*|EPIC)\b/g)) {
    lsaRefs++
    if (!keys.has(m[1]) && !rel.includes('/reports/')) problems.push(`${rel}: unknown LSA-${m[1]}`)
  }

  // 2.
  let trackerRefs = 0
  if (tracker) {
    for (const m of text.matchAll(/`((?:1ir|1kg|xiu|yje|1ka|b8o|iu6|va8|x5bz|764)(?:\.\d+)*)`/g)) {
      trackerRefs++
      if (!trackerIds.has(m[1])) problems.push(`${rel}: unknown tracker bead ${m[1]}`)
    }
  }

  // 3.
  let inFence = false
  let tableCols = 0
  let tableStart = 0
  const cells = (l: string) => l.trim().replace(/^\|/, '').replace(/\|$/, '').replace(/\\\|/g, '').replace(/`[^`]*`/g, 'x').split('|').length
  lines.forEach((l, i) => {
    if (/^\s*```/.test(l)) inFence = !inFence
    if (inFence) return
    if (/^\s*\|/.test(l)) {
      const n = cells(l)
      if (!tableCols) { tableCols = n; tableStart = i + 1 }
      else if (n !== tableCols) problems.push(`${rel}:${i + 1}: table row has ${n} cells, header at ${tableStart} has ${tableCols}`)
    } else tableCols = 0
  })

  // 4.
  for (const m of text.matchAll(/`((?:docs|service|ui|scripts|vector-db|ingestion)\/[A-Za-z0-9_./-]+?\.(?:md|py|ts|tsx|sql|sh|conf|json|toml))(?::\d+(?:[-,]\d+)*)?`/g)) {
    if (existsSync(join(REPO_ROOT, m[1])) || PENDING_ELSEWHERE.some((p) => m[1].endsWith(p))) continue
    const onMaster = Bun.spawnSync(['git', 'cat-file', '-e', `origin/master:${m[1]}`], { cwd: REPO_ROOT }).exitCode === 0
    if (!onMaster) problems.push(`${rel}: path not found ${m[1]}`)
  }

  // 5.
  if (inFence) problems.push(`${rel}: unterminated code fence`)
  stats[rel] = lines.length
  stats[`${rel} LSA refs`] = lsaRefs
  stats[`${rel} tracker refs`] = trackerRefs
}

// 6.
const tm = readFileSync(join(REPO_ROOT, DOCS[2]), 'utf8')
const defined = new Set([...tm.matchAll(/^\| (TM-\d+) \|/gm)].map((m) => m[1]))
for (const m of tm.matchAll(/TM-(\d+)((?:, \d+)*)/g)) {
  const nums = [m[1], ...(m[2] ? m[2].split(',').map((s) => s.trim()).filter(Boolean) : [])]
  for (const n of nums) if (!defined.has(`TM-${n.padStart(2, '0')}`)) problems.push(`threat model cites undefined TM-${n}`)
}
stats['threats defined'] = defined.size

console.log(JSON.stringify(stats, null, 2))
if (!tracker) console.log('\nno tracker available: skipped the bead ID check')
if (problems.length) { console.log(`\n${problems.length} problem(s):`); for (const p of problems) console.log(' -', p); process.exit(1) }
console.log('\nOK: no problems')
