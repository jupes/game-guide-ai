// Checks for the account identity record (yje.1.6), run by checkdocs.ts:
//   a. its ids (first cells of table rows, `**X-n:` rules and `- **X-n ` bullets) are defined once, every citation of
//      a family it defines resolves, and no other decision record under docs/adr defines the same id;
//   b. the invariants its first review found broken stay fixed (PR #106, findings H-1 to H-3 and M-1 to M-4).
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

export const IDENTITY_DOC = 'docs/adr/account-identity-state-machine.md'

const ID = '[A-Z][A-Z0-9]*-\\d+'

/** Ids a decision record defines: the first cell of a table row, or a bold definition such as `**R-1:` or `- **Z-1 `. */
export function definedIds(text: string): string[] {
  const out: string[] = []
  for (const m of text.matchAll(new RegExp(`^\\|\\s*\\**(${ID})\\**\\s*\\|`, 'gm'))) out.push(m[1])
  for (const m of text.matchAll(new RegExp(`^(?:- )?\\*\\*(${ID})(?::| [A-Za-z])`, 'gm'))) out.push(m[1])
  return out
}

/** Body rows (cells trimmed) of the first table whose header line matches `header`. */
function table(text: string, header: RegExp): string[][] {
  const lines = text.split(/\r?\n/)
  const start = lines.findIndex((l) => header.test(l))
  const rows: string[][] = []
  if (start < 0) return rows
  for (let i = start + 2; i < lines.length && /^\s*\|/.test(lines[i]); i++) {
    rows.push(lines[i].trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim()))
  }
  return rows
}

/** Every id a clause names, with `X-3 to X-5` expanded. */
function idsIn(clause: string): Set<string> {
  const out = new Set<string>()
  for (const m of clause.matchAll(new RegExp(`(${ID})(?: to (${ID}))?`, 'g'))) {
    out.add(m[1])
    const [fam, lo] = [m[1].replace(/-\d+$/, ''), Number(m[1].split('-').pop())]
    if (m[2]?.startsWith(`${fam}-`)) for (let n = lo + 1; n <= Number(m[2].split('-').pop()); n++) out.add(`${fam}-${n}`)
  }
  return out
}

export function checkIdentity(root: string): string[] {
  const text = readFileSync(join(root, IDENTITY_DOC), 'utf8')
  const problems = new Set<string>()
  const p = (s: string) => problems.add(`${IDENTITY_DOC}: ${s}`)

  // a. Ids: unique here, every citation resolves, none shared with a sibling record.
  const defined = new Set<string>()
  for (const id of definedIds(text)) { if (defined.has(id)) p(`${id} is defined twice`); defined.add(id) }
  const families = [...new Set([...defined].map((id) => id.replace(/-\d+$/, '')))].sort((a, b) => b.length - a.length)
  if (families.length) {
    for (const m of text.matchAll(new RegExp(`(?<![A-Za-z0-9-])((?:${families.join('|')})-\\d+)\\b`, 'g'))) {
      if (!defined.has(m[1])) p(`cites ${m[1]}, which it does not define`)
    }
  }
  const adr = join(root, 'docs', 'adr')
  for (const f of readdirSync(adr).sort()) {
    if (!f.endsWith('.md') || `docs/adr/${f}` === IDENTITY_DOC) continue
    const theirs = new Set(definedIds(readFileSync(join(adr, f), 'utf8')))
    const shared = [...defined].filter((id) => theirs.has(id))
    if (shared.length) p(`ids also defined by docs/adr/${f}: ${shared.join(', ')}`)
  }

  // b1 (H-1). The refusal marker is written by script (no request is sent), and a cookie written by script with
  // HttpOnly is dropped by the browser (RFC 6265 section 5.3, step 10), so its attribute list must not carry it.
  const marker = table(text, /^\| Item \| Where \|/).find((r) => r[0].startsWith('Refusal marker'))
  const attrs = marker ? [...marker[1].matchAll(/`([^`]*Path=[^`]*)`/g)].map((m) => m[1]) : []
  if (!attrs.length) p('the refusal marker names no cookie attribute list (a backticked `...; Path=...`)')
  for (const a of attrs) if (/httponly/i.test(a)) p(`the refusal marker is set by script, so it cannot be HttpOnly: \`${a}\``)

  // b2 (H-2). A closure nobody triggers (no bold trigger, not from Deleted) is terminal, so it may read only facts
  // that section 3.2 says are never cleared, and must read at least one.
  const cleared = new Map<string, string>()
  for (const r of table(text, /^\| Column \(indicative\) \| Set by \| Cleared by \|/)) {
    for (const m of r[0].matchAll(/`([^`]+)`/g)) cleared.set(m[1], r[2] ?? '')
  }
  const transitions = table(text, /^\| # \| From \| Trigger \| Guard \| To \| Effects \|/)
  if (!transitions.length) p('no transition table')
  for (const r of transitions.filter((r) => /^Deleted\b/.test(r[4] ?? '') && !/^Deleted/.test(r[1]) && !r[2].includes('**'))) {
    const reads = [...`${r[2]} ${r[3]}`.matchAll(/`([^`]+)`/g)].map((m) => m[1]).filter((c) => cleared.has(c))
    if (!reads.length) p(`${r[0]} closes an account unattended without reading any fact section 3.2 holds`)
    for (const c of reads) if (!/^never/i.test(cleared.get(c) ?? '')) p(`${r[0]} closes an account unattended on \`${c}\`, which "${cleared.get(c)}" clears`)
  }

  // b3 (H-3). A completed reset hands the account to whoever holds the mailbox, so it revokes every session and
  // deletes every outstanding token, and yje.2.3's revocation list names it.
  const resets = transitions.filter((r) => /password reset/i.test(r[2] ?? ''))
  if (!resets.length) p('no transition is triggered by a completed password reset')
  const y23 = table(text, /^\| Bead \| Obligation \|/).find((r) => r[0] === '`yje.2.3`')
  const listed = idsIn(/revocation on ([^;]*)/.exec(y23?.[1] ?? '')?.[1] ?? '')
  for (const r of resets) {
    if (!/every session/i.test(r[5] ?? '') || !/every outstanding token/i.test(r[5] ?? '')) {
      p(`${r[0]}: a completed reset must revoke every session and delete every outstanding token`)
    }
    if (!listed.has(r[0])) p(`yje.2.3's revocation list omits ${r[0]}, the completed reset`)
  }

  // b4 (M-1). Completing a mailed link is self-service, so a state whose section 9 self-service is "None" names every
  // link or reset transition that may start from it.
  const answers = table(text, /^\| State \| May sign in \|/)
  for (const r of transitions.filter((r) => /link|reset/i.test(r[2] ?? ''))) {
    for (const a of answers) {
      const state = a[0].replace(/\*/g, '')
      if (r[1].includes(state) && /^None/.test(a[5] ?? '') && !a[5].includes(r[0])) {
        p(`${r[0]} may start from ${state}, whose self-service in section 9 is "None" without naming it`)
      }
    }
  }

  // b5 (M-2). Section 4.4 keeps nothing of a child: an under-age closure scrubs the address and the password hash.
  const ageClosures = transitions.filter((r) => /^Deleted\b/.test(r[4] ?? '') && /reason `age`/.test(r[5] ?? ''))
  if (!ageClosures.length) p('no closure names reason `age`')
  for (const r of ageClosures) if (!/scrubs the address and the password hash/.test(r[5])) p(`${r[0]}: an under-age closure must scrub the address and the password hash`)

  // b6 (M-3). A lift ends any table session the account still runs, so one started in a race's gap never serves again.
  const lifts = transitions.filter((r) => /^Suspended$/.test(r[1] ?? '') && /Unverified or Verified/.test(r[4] ?? ''))
  if (!lifts.length) p('no lift transition')
  for (const r of lifts) if (!/still live[^|]*ended by the lift/.test(r[5] ?? '')) p(`${r[0]}: a lift must end a table session that is still live`)

  // b7 (M-4). The lock order after the account row is stated, so two transitions cannot deadlock.
  const lockRule = /^\*\*IDR-1:[\s\S]*?(?:\r?\n){2}/m.exec(text)?.[0] ?? ''
  if (!/ascending campaign id/.test(lockRule)) p('IDR-1 does not order the campaign locks it takes after the account row')
  return [...problems]
}
