/**
 * The Zod half of the differential fuzz. `differential_fuzz.py` mutates the
 * valid fixtures, records the server's verdict on each mutation — and, for the
 * ones it accepts, what it would emit — and calls this with the result.
 *
 *   bun run differential_fuzz.ts <repo root> <cases.json> [--verbose]     (from ui/)
 *
 * Exit status 1 when either property fails:
 *   1. the two validators agree about the contract's own shapes (disagreements
 *      inside the reused /chat models are counted, and listed with --verbose);
 *   2. the client can read everything the server emits.
 * See the Python half for the reasoning.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

interface Case {
  schema: string
  example: string
  path: string
  how: string
  value: unknown
  server: boolean
  /** What the server would send for a case it accepts. */
  emitted: unknown
}

interface SafeParser {
  safeParse: (value: unknown) => { success: boolean }
}

const [root, casesFile] = process.argv.slice(2)
if (!root || !casesFile) {
  console.error('usage: bun run differential_fuzz.ts <repo root> <cases.json> [--verbose]')
  process.exit(2)
}

const cases = JSON.parse(readFileSync(casesFile, 'utf-8')) as Case[]
const contracts = (await import(join(root, 'ui', 'src', 'gm', 'contracts.ts'))) as {
  CONTRACT_SCHEMAS: Record<string, SafeParser>
}

/** Paths that run through service/models.py shapes the contract reuses as they
 * are: lax by an older contract's design, and normalised when emitted. */
const INHERITED = /(^|\.)(sources|stat_block|spell_content|routing|suggestions_routing)(\.|$)|(^|\.)answer\.suggestions(\.|$)/

const findings = new Map<string, number>()
const unreadable: string[] = []
let agreed = 0
let inherited = 0
let emitted = 0

for (const item of cases) {
  const schema = contracts.CONTRACT_SCHEMAS[item.schema]
  if (item.server) {
    emitted += 1
    if (!schema.safeParse(item.emitted).success) {
      unreadable.push(`${item.schema} | ${item.path} | ${item.how} | ${item.example}`)
    }
  }

  const client = schema.safeParse(item.value).success
  if (client === item.server) {
    agreed += 1
    continue
  }
  const owner = INHERITED.test(item.path) ? 'inherited' : 'CONTRACT'
  if (owner === 'inherited') inherited += 1
  // Indexes drop out, so that one finding in a list is reported once.
  const field = item.path.split('.').filter((part) => !/^\d+$/.test(part)).slice(-2).join('.')
  const key = `${owner} | ${item.schema} | ${field} | ${item.how} | server ${item.server ? 'accepts' : 'rejects'}, client ${client ? 'accepts' : 'rejects'}`
  findings.set(key, (findings.get(key) ?? 0) + 1)
}

const disagreed = cases.length - agreed
const ours = disagreed - inherited
console.log(`${agreed} agree; ${disagreed} disagree — ${ours} in the contract's own shapes, ${inherited} inside the reused /chat models`)
const verbose = process.argv.includes('--verbose')
for (const [key, count] of [...findings.entries()].sort()) {
  // The inherited ones are known; listing them would bury a real finding.
  if (verbose || key.startsWith('CONTRACT')) console.log(`${String(count).padStart(5)}  ${key}`)
}

console.log(`${emitted} accepted cases re-emitted by the server; the client cannot read ${unreadable.length}`)
for (const line of unreadable.sort()) console.log(`       UNREADABLE | ${line}`)

if (ours > 0) {
  console.error('\nThe two validators disagree. Decide which is right, fix the other, and pin the case with a fixture example.')
}
if (unreadable.length > 0) {
  console.error('\nThe server can emit something the client cannot read. That is a wire break, whichever side is lax.')
}
if (ours > 0 || unreadable.length > 0) process.exit(1)
