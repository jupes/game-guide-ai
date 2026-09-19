/**
 * canvasStyles — the stylesheet guard for the canvas (1kg.6.1).
 *
 * Reads `CanvasPane.css` and `VersionList.css` as text, the way
 * `ds/contrast.test.ts` reads the token files, and asserts three things jsdom
 * cannot see because it does not evaluate CSS:
 *
 * 1. **Tokens only.** No literal colour anywhere, so light and dark both follow
 *    the palette and a future retone cannot leave one theme behind.
 * 2. **LAYOUT-9 · reduced motion.** Every selector that animates or transitions
 *    is neutralised under `prefers-reduced-motion: reduce` — the gold wash
 *    included (CANVAS-28).
 * 3. **LAYOUT-9 · the 44 px touch floor**, and a visible focus ring, on every
 *    control these files own. The ds Button and IconButton hold their own.
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

const SHEETS: [string, string][] = [
  ['CanvasPane.css', readFileSync(join(HERE, 'CanvasPane.css'), 'utf8')],
  ['VersionList.css', readFileSync(join(HERE, 'VersionList.css'), 'utf8')],
]

const REDUCE_MOTION = '@media (prefers-reduced-motion: reduce)'

interface Rule {
  selectors: string[]
  declarations: [string, string][]
}

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

/** The body of the block that opens at `from`, and the index just past its `}`. */
function block(css: string, from: number): { body: string; end: number } {
  const open = css.indexOf('{', from)
  let depth = 0
  for (let at = open; at < css.length; at += 1) {
    if (css[at] === '{') depth += 1
    else if (css[at] === '}') {
      depth -= 1
      if (depth === 0) return { body: css.slice(open + 1, at), end: at + 1 }
    }
  }
  return { body: css.slice(open + 1), end: css.length }
}

/** Splits a sheet into the at-rule-free part and the reduced-motion block. */
function split(css: string): { flat: string; reduced: string } {
  const clean = stripComments(css)
  let flat = ''
  let reduced = ''
  let at = 0
  while (at < clean.length) {
    const next = clean.indexOf('@', at)
    if (next === -1) {
      flat += clean.slice(at)
      break
    }
    flat += clean.slice(at, next)
    const { body, end } = block(clean, next)
    if (clean.slice(next, end).startsWith(REDUCE_MOTION)) reduced += body
    at = end
  }
  return { flat, reduced }
}

function rules(css: string): Rule[] {
  const parsed: Rule[] = []
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = match[1]
      .split(',')
      .map((selector) => selector.trim().replace(/\s+/g, ' '))
      .filter((selector) => selector.length > 0)
    const declarations = match[2]
      .split(';')
      .map((declaration) => declaration.trim())
      .filter((declaration) => declaration.includes(':'))
      .map((declaration): [string, string] => {
        const colon = declaration.indexOf(':')
        return [declaration.slice(0, colon).trim(), declaration.slice(colon + 1).trim()]
      })
    parsed.push({ selectors, declarations })
  }
  return parsed
}

// ── 1. Tokens only ───────────────────────────────────────────────────────────

const COLOUR_PROPERTIES = new Set([
  'color',
  'background',
  'background-color',
  'background-image',
  'border',
  'border-top',
  'border-right',
  'border-bottom',
  'border-left',
  'border-color',
  'outline',
  'outline-color',
  'box-shadow',
  'fill',
  'stroke',
  'caret-color',
  'accent-color',
  'text-decoration-color',
])

/** Values that name no colour at all, so they need no token. */
const COLOURLESS = /^(none|transparent|currentcolor|inherit|initial|unset|revert|0)$/i

describe('tokens only — neither theme is hard-coded', () => {
  for (const [name, css] of SHEETS) {
    it(`${name} holds no literal colour`, () => {
      const clean = stripComments(css)
      expect(clean.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [], 'hex literals').toEqual([])
      expect(clean.match(/\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix)\s*\(/g) ?? [], 'colour functions').toEqual([])
    })

    it(`${name} paints every colour-bearing declaration from a token`, () => {
      const { flat, reduced } = split(css)
      const offenders: string[] = []
      let inspected = 0
      for (const rule of [...rules(flat), ...rules(reduced)]) {
        for (const [property, value] of rule.declarations) {
          if (!COLOUR_PROPERTIES.has(property)) continue
          inspected += 1
          if (COLOURLESS.test(value)) continue
          if (value.includes('var(--')) continue
          offenders.push(`${rule.selectors.join(', ')} { ${property}: ${value} }`)
        }
      }
      expect(offenders).toEqual([])
      // The sweep above is only meaningful if it actually saw the sheet.
      expect(inspected).toBeGreaterThan(5)
    })

    it(`${name} overrides no theme by hand`, () => {
      expect(stripComments(css)).not.toContain('data-theme')
    })
  }
})

// ── 2. LAYOUT-9 · reduced motion ─────────────────────────────────────────────

describe('LAYOUT-9 — every motion is answered under prefers-reduced-motion', () => {
  for (const [name, css] of SHEETS) {
    it(`${name} declares a reduced-motion block`, () => {
      expect(stripComments(css)).toContain(REDUCE_MOTION)
    })

    it(`${name} neutralises every animated selector there`, () => {
      const { flat, reduced } = split(css)
      const off = new Map<string, Set<string>>()
      for (const rule of rules(reduced)) {
        for (const selector of rule.selectors) {
          const already = off.get(selector) ?? new Set<string>()
          for (const [property, value] of rule.declarations) {
            if ((property === 'animation' || property === 'transition') && value === 'none') already.add(property)
          }
          off.set(selector, already)
        }
      }

      const unanswered: string[] = []
      for (const rule of rules(flat)) {
        for (const [property, value] of rule.declarations) {
          if (property !== 'animation' && property !== 'transition') continue
          if (value === 'none') continue
          for (const selector of rule.selectors) {
            if (off.get(selector)?.has(property) !== true) unanswered.push(`${selector} { ${property} }`)
          }
        }
      }
      expect(unanswered).toEqual([])
    })

    it(`${name} animates something, so the guard above is not vacuous`, () => {
      const { flat } = split(css)
      const animated = rules(flat).filter((rule) =>
        rule.declarations.some(([property, value]) => (property === 'animation' || property === 'transition') && value !== 'none'),
      )
      expect(animated.length).toBeGreaterThan(0)
    })
  }
})

// ── 3. LAYOUT-9 · touch floor and visible focus ──────────────────────────────

const TOUCH_TARGETS: [string, string][] = [
  ['CanvasPane.css', '.gm-canvas__more'],
  ['CanvasPane.css', '.gm-canvas__menu-item'],
  ['VersionList.css', '.gm-versions__row'],
  ['VersionList.css', '.gm-versions__load-more'],
]

const FOCUS_RINGS: [string, string][] = [
  ['CanvasPane.css', '.gm-canvas__more:focus-visible'],
  ['CanvasPane.css', '.gm-canvas__menu-item:focus-visible'],
  ['CanvasPane.css', '.gm-canvas__body:focus-visible'],
  ['VersionList.css', '.gm-versions__row:focus-visible'],
  ['VersionList.css', '.gm-versions__load-more:focus-visible'],
]

function declarationsFor(sheet: string, selector: string): [string, string][] {
  const css = SHEETS.find(([name]) => name === sheet)?.[1] ?? ''
  const { flat } = split(css)
  return rules(flat)
    .filter((rule) => rule.selectors.includes(selector))
    .flatMap((rule) => rule.declarations)
}

describe('LAYOUT-9 — the 44 px touch floor and a visible focus ring', () => {
  for (const [sheet, selector] of TOUCH_TARGETS) {
    it(`${selector} holds the touch floor`, () => {
      const declarations = declarationsFor(sheet, selector)
      expect(declarations.length, `${selector} is missing from ${sheet}`).toBeGreaterThan(0)
      const floor = declarations.find(([property]) => property === 'min-height')
      expect(floor?.[1], `${selector} sets no min-height`).toBe('var(--aether-touch-min)')
    })
  }

  for (const [sheet, selector] of FOCUS_RINGS) {
    it(`${selector} draws a focus ring`, () => {
      const declarations = declarationsFor(sheet, selector)
      const outline = declarations.find(([property]) => property === 'outline')
      expect(outline?.[1], `${selector} draws no outline in ${sheet}`).toContain('var(--md-sys-color-secondary)')
    })
  }

  it('the touch floor is the design system’s token, not a repeated number', () => {
    for (const [, css] of SHEETS) {
      expect(stripComments(css)).not.toMatch(/min-height:\s*44px/)
    }
  })
})
