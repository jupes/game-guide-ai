/**
 * The stylesheet guard for the document renderers (agent-forge-harness-1kg.6.2).
 *
 * jsdom evaluates no CSS, so the rules a rendered test cannot see are read here
 * as text — the same way `canvasStyles.test.ts` reads the canvas and
 * `ds/contrast.test.ts` reads the palette:
 *
 * 1. **Tokens only**, so light and dark both follow the palette and a retone
 *    cannot leave one theme behind.
 * 2. **LAYOUT-9 · reduced motion** — everything that moves is neutralised under
 *    `prefers-reduced-motion: reduce`.
 * 3. **LAYOUT-9 · the 44 px touch floor and a visible focus ring** on every
 *    control these files own. The ds `Button` holds its own.
 * 4. **§10.2** — the document still works in a 400 px canvas.
 *
 * `import.meta.url` is resolved with `fileURLToPath` rather than `new URL`,
 * because jsdom's `import.meta.url` is not a file URL.
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

const SHEETS: [string, string][] = [
  ['GameDocument.css', readFileSync(join(HERE, 'GameDocument.css'), 'utf8')],
  ['DocumentField.css', readFileSync(join(HERE, 'DocumentField.css'), 'utf8')],
  ['SelectionBar.css', readFileSync(join(HERE, 'SelectionBar.css'), 'utf8')],
]

const REDUCE_MOTION = '@media (prefers-reduced-motion: reduce)'

interface Rule {
  selectors: string[]
  declarations: [string, string][]
}

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

/** The body of the block that opens at `from`, and the index past its `}`. */
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

/** The at-rule-free part of a sheet, its reduced-motion block, and its media
 * queries with their conditions. */
function split(css: string): { flat: string; reduced: string; queries: string[] } {
  const clean = stripComments(css)
  let flat = ''
  let reduced = ''
  const queries: string[] = []
  let at = 0
  while (at < clean.length) {
    const next = clean.indexOf('@', at)
    if (next === -1) {
      flat += clean.slice(at)
      break
    }
    flat += clean.slice(at, next)
    const { body, end } = block(clean, next)
    const head = clean.slice(next, clean.indexOf('{', next)).trim()
    queries.push(head)
    if (head === REDUCE_MOTION) reduced += body
    at = end
  }
  return { flat, reduced, queries }
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
  'border-inline-start',
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

const COLOURLESS = /^(none|transparent|currentcolor|inherit|initial|unset|revert|0)$/i

describe('tokens only — neither theme is hard-coded', () => {
  for (const [name, css] of SHEETS) {
    it(`${name} holds no literal colour`, () => {
      const clean = stripComments(css)
      expect(clean.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [], 'hex literals').toEqual([])
      expect(clean.match(/\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix)\s*\(/g) ?? [], 'colour functions').toEqual(
        [],
      )
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
      // The sweep is only meaningful if it actually saw the sheet.
      expect(inspected).toBeGreaterThan(4)
    })

    it(`${name} overrides no theme by hand`, () => {
      expect(stripComments(css)).not.toContain('data-theme')
    })
  }
})

// ── 1b. X-10 · no sheet fetches anything ─────────────────────────────────────

/**
 * The one leak a rendered test can never see.
 *
 * jsdom evaluates no CSS, so `background-image: url(https://cdn.example/x.png)`
 * in any of these sheets would load a remote image in a real browser, carry
 * the GM's referrer to a third party, and leave every DOM sweep in this suite
 * green. `@import` is the same hole with a stylesheet on the end of it, and
 * `image-set()`, `src` and `cursor` all take a URL too. These sheets need no
 * external resource at all — the icon font is the app's, loaded once in
 * `index.html` — so the honest rule is: none, of any kind, relative included.
 */
describe('X-10 — no sheet fetches anything, from anywhere', () => {
  for (const [name, css] of SHEETS) {
    it(`${name} references no URL`, () => {
      const clean = stripComments(css)
      expect(clean.match(/\burl\s*\(/gi) ?? [], `${name}: url()`).toEqual([])
      expect(clean.match(/\bimage-set\s*\(/gi) ?? [], `${name}: image-set()`).toEqual([])
    })

    it(`${name} imports no other stylesheet`, () => {
      // `split` only sees an at-rule that opens a block, and a bare
      // `@import "…";` has no braces — so this is read from the text.
      expect(stripComments(css)).not.toMatch(/@import\b/i)
    })

    it(`${name} names no host`, () => {
      expect(stripComments(css)).not.toMatch(/\/\/[a-z0-9-]+(\.[a-z0-9-]+)+/i)
    })
  }
})

// ── 2. LAYOUT-9 · reduced motion ─────────────────────────────────────────────

describe('LAYOUT-9 — every motion is answered under prefers-reduced-motion', () => {
  for (const [name, css] of SHEETS) {
    it(`${name} neutralises every animated selector`, () => {
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
        rule.declarations.some(
          ([property, value]) => (property === 'animation' || property === 'transition') && value !== 'none',
        ),
      )
      expect(animated.length).toBeGreaterThan(0)
    })
  }
})

// ── 3. LAYOUT-9 · the touch floor and a visible focus ring ───────────────────

const TOUCH_TARGETS: [string, string][] = [
  ['DocumentField.css', '.gm-field__icon'],
  ['DocumentField.css', '.gm-field__input'],
  ['DocumentField.css', '.gm-field__text-button'],
  ['SelectionBar.css', '.gm-selection-bar__action'],
]

const FOCUS_RINGS: [string, string][] = [
  ['DocumentField.css', '.gm-field__icon:focus-visible'],
  ['DocumentField.css', '.gm-field__input:focus-visible'],
  ['DocumentField.css', '.gm-field__text-button:focus-visible'],
  // CANVAS-23: the read presentation of an editable prose field is a tab stop,
  // so it needs a ring of its own — the selection is made from there.
  ['DocumentField.css', '.gm-field__prose:focus-visible'],
  ['SelectionBar.css', '.gm-selection-bar__action:focus-visible'],
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
      const outline = declarationsFor(sheet, selector).find(([property]) => property === 'outline')
      expect(outline?.[1], `${selector} draws no outline in ${sheet}`).toContain('var(--md-sys-color-secondary)')
    })
  }

  it('the touch floor is the design system’s token, not a repeated number', () => {
    for (const [, css] of SHEETS) expect(stripComments(css)).not.toMatch(/min-height:\s*44px/)
  })
})

// ── 4. §10.2 · a narrow canvas ───────────────────────────────────────────────

describe('§10.2 — the document still works in a narrow canvas', () => {
  it('drops the stat strip to one column below 768px', () => {
    expect(stripComments(SHEETS[0][1])).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.gm-document__stats\s*\{[\s\S]*?grid-template-columns: minmax\(0, 1fr\)/,
    )
  })

  it('stacks a conflict rather than squeezing two values side by side', () => {
    expect(stripComments(SHEETS[1][1])).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.gm-field__conflict\s*\{[\s\S]*?grid-template-columns: minmax\(0, 1fr\)/,
    )
  })

  it('gives the SelectionBar the document as its containing block', () => {
    // Without this the bar would be placed against the viewport, and the
    // clamp that keeps it inside a 400 px pane would be measuring the wrong box.
    const document = declarationsFor('GameDocument.css', '.gm-document')
    expect(document.find(([property]) => property === 'position')?.[1]).toBe('relative')
    const bar = declarationsFor('SelectionBar.css', '.gm-selection-bar')
    expect(bar.find(([property]) => property === 'position')?.[1]).toBe('absolute')
  })
})
