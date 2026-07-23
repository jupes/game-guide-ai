/**
 * tokenIntegrity — guards against the swe1.1/swe1.2 root cause: shell CSS
 * referencing an undefined custom-property namespace.
 *
 * The Aetheril design system exposes Material-3 color roles as
 * `--md-sys-color-<role>`; the `--aether-*` prefix is reserved for extensions
 * (arcane, dice states, spacing, radius, fonts). The shell components had
 * regressed to `var(--aether-surface-container)` etc. — tokens that are never
 * defined, so `color` fell back to the inherited value and `background` to
 * transparent. That killed the selected-conversation highlight (swe1.2) and the
 * muted text tokens (swe1.1) with no test to catch it.
 *
 * This test resolves every no-fallback `var(--token)` used in shell/*.css
 * against the set of custom properties actually defined across the design
 * system, and fails listing any that resolve to nothing.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const SHELL_DIR = dirname(fileURLToPath(import.meta.url)) // ui/src/shell
const SRC_DIR = join(SHELL_DIR, '..') // ui/src

/** Recursively collect every *.css file under a directory. */
function cssFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...cssFiles(full))
    else if (entry.name.endsWith('.css')) out.push(full)
  }
  return out
}

/** Custom properties DEFINED anywhere in the design system (`--x: value;`). */
function definedTokens(): Set<string> {
  const defs = new Set<string>()
  for (const file of cssFiles(SRC_DIR)) {
    const css = readFileSync(file, 'utf8')
    for (const m of css.matchAll(/(--[a-z0-9-]+)\s*:/gi)) defs.add(m[1])
  }
  return defs
}

/**
 * No-fallback references `var(--token)` in a stylesheet. References that carry
 * a fallback — `var(--token, 16px)` — are intentionally tolerant and excluded.
 */
function noFallbackRefs(css: string): string[] {
  const refs: string[] = []
  for (const m of css.matchAll(/var\(\s*(--[a-z0-9-]+)\s*\)/gi)) refs.push(m[1])
  return refs
}

describe('shell CSS token integrity', () => {
  const defined = definedTokens()

  for (const file of cssFiles(SHELL_DIR)) {
    const name = file.slice(file.indexOf('shell'))
    it(`${name} references only defined custom properties`, () => {
      const undefinedRefs = [...new Set(noFallbackRefs(readFileSync(file, 'utf8')))]
        .filter((token) => !defined.has(token))
      expect(undefinedRefs, `${name} uses undefined tokens: ${undefinedRefs.join(', ')}`).toEqual([])
    })
  }
})
