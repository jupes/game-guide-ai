/**
 * agent-forge-harness-hxq (pr128 F1). `.storybook/touchTarget.ts` re-asserts
 * the 44px touch floor at each story's own rendered call site, but a story's
 * iframe only loads the CSS its OWN component imports — it never sees a rule
 * that some unrelated component's stylesheet writes for `.aether-btn` /
 * `.aether-icon-btn`. WorkspaceShell.tsx renders LeftNav and ChatPane
 * together in the real app, so a shrink written in e.g. ChatPane.css would
 * still shrink LeftNav's buttons there, even though every storybook story —
 * LeftNav's own included — stays green.
 *
 * Mutant M15 (scratchpad review/lean/pr128.md) proved this: appending a
 * shrinking rule for `.aether-btn[data-size="small"]` /
 * `.aether-icon-btn[data-size="small"]` to shell/ChatPane.css left the full
 * storybook project at 44 files / 404 passed.
 *
 * This is the static guard for that gap: no *.css file outside `ds/Button.css`
 * and `ds/IconButton.css` (the only two places the 44px floor is allowed to be
 * declared) may set a physical or logical min/size property on a selector
 * that names `.aether-btn`/`.aether-icon-btn`, a bare `button` type selector,
 * or `[data-size`. jsdom does not evaluate CSS or load stylesheets together
 * the way a real page does, so — like audioStyles.test.ts — this reads the
 * files directly instead of rendering them.
 *
 * agent-forge-harness-opn (#138 M-1): the original guard only matched
 * `.aether-btn`/`.aether-icon-btn` by name and only the four physical size
 * properties, and exempted every file under src/ds/, not just the two that
 * actually own the floor. `.left-nav button[data-size="small"] { min-height:
 * 0 }` shrinks the exact same rendered elements the class-name check protects
 * — Button/IconButton stamp `data-size` on themselves and are real `<button>`
 * elements — but named neither `.aether-btn` nor `.aether-icon-btn`, so it
 * passed; so did the same shrink written with `min-block-size`/
 * `min-inline-size` instead of `min-height`/`min-width`; so would a shrink
 * written into any other src/ds/*.css file, since the whole directory was
 * exempt rather than just the two files the floor belongs in.
 *
 * agent-forge-harness-8ug (#147 M-2): every file this guard actually scans is
 * real production CSS with zero violations, so a mutant that guts the
 * detection itself -- `isGuardedSelector` always `false`, `SIZE_PROPERTIES`
 * emptied, the `blocks()` regex broken -- passes exactly as green as the
 * unmutated code; there was no committed fixture with a real violation to
 * prove the scan can ever find one. `__fixtures__/` below is excluded from
 * the production sweep (like the two floor owners) and holds one, so the
 * positive-control test at the bottom exercises every guarded-selector kind
 * and every size property against real detection logic, not mocks.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'

const SRC_DIR = dirname(dirname(fileURLToPath(import.meta.url)))

/** The only two files the 44px floor is allowed to live in. */
const FLOOR_OWNERS = [join(SRC_DIR, 'ds', 'Button.css'), join(SRC_DIR, 'ds', 'IconButton.css')]

/** Directory name for committed violation fixtures (see the bottom of this
 * file) — deliberately outside the production sweep, the same way the two
 * floor owners are, so a fixture proving the scan CAN fire never trips the
 * scan it exists to prove. */
const FIXTURE_DIR_NAME = '__fixtures__'

/** Every *.css file under src/, outside the floor's two legitimate owners and
 * the fixtures directory. */
function cssFilesOutsideFloorOwners(dir: string): string[] {
  const found: string[] = []
  for (const name of readdirSync(dir)) {
    if (name === FIXTURE_DIR_NAME) continue
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      found.push(...cssFilesOutsideFloorOwners(full))
    } else if (name.endsWith('.css') && !FLOOR_OWNERS.includes(full)) {
      found.push(full)
    }
  }
  return found
}

const GUARDED_FILES = cssFilesOutsideFloorOwners(SRC_DIR)

/** A committed fixture with one deliberate violation per guarded-selector
 * kind and per size property -- see the positive-control test below. */
const VIOLATION_FIXTURE = join(SRC_DIR, 'ds', FIXTURE_DIR_NAME, 'touchTargetLeak.violation.css')

/** CSS comments hold prose, not declarations. */
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, ' ')

/** A selector names the button classes this guard protects — not a BEM part
 * of them, such as `.aether-btn__state` or `.aether-icon-btn__label`. */
const NAMES_GUARDED_CLASS = /\.aether-(?:icon-)?btn(?![\w-])/

/** A bare `button` type selector, e.g. `.left-nav button[data-size="small"]`
 * or `.foo > button:hover` — not a class whose name merely contains the
 * substring, such as `.send-button` (a hyphen still starts a word boundary,
 * so this must additionally require what follows "button" to end the token:
 * whitespace, a combinator, or a compounding `.`/`[`/`:`, never `-`). */
const NAMES_BUTTON_ELEMENT = /(?:^|[\s,>+~(])button(?=[\s,>+~).:[\],]|$)/

/** Button/IconButton stamp their own size tier on themselves as `data-size`;
 * nothing else in this design system uses that attribute. */
const NAMES_DATA_SIZE_ATTR = /\[data-size\b/

const isGuardedSelector = (selector: string): boolean =>
  NAMES_GUARDED_CLASS.test(selector) || NAMES_BUTTON_ELEMENT.test(selector) || NAMES_DATA_SIZE_ATTR.test(selector)

/** Physical and logical size properties both floor or shrink a target; a
 * writing-mode-aware shrink through min-block-size/min-inline-size is exactly
 * as real as one through min-height/min-width (#138 M-1). */
const SIZE_PROPERTIES = [
  'min-height',
  'min-width',
  'height',
  'width',
  'min-block-size',
  'min-inline-size',
  'block-size',
  'inline-size',
]

interface Block {
  selector: string
  body: string
}

/** Top-level `selector { ... }` blocks. This repo's stylesheets do not nest
 * rules, so a non-nesting scan is enough (the same assumption audioStyles's
 * declarations() scan makes). */
function blocks(css: string): Block[] {
  const found: Block[] = []
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    found.push({ selector: match[1], body: match[2] })
  }
  return found
}

it('found the files it is meant to guard', () => {
  expect(GUARDED_FILES.length).toBeGreaterThanOrEqual(30)
  expect(GUARDED_FILES.some((f) => f.endsWith('ChatPane.css'))).toBe(true)
  expect(GUARDED_FILES.some((f) => f.endsWith('LeftNav.css'))).toBe(true)
  expect(GUARDED_FILES).not.toContain(join(SRC_DIR, 'ds', 'Button.css'))
  expect(GUARDED_FILES).not.toContain(join(SRC_DIR, 'ds', 'IconButton.css'))
  // #138 M-1: the exclusion is narrowed to the floor's two owners, not the
  // whole ds/ directory — every other src/ds/*.css file is guarded too.
  expect(GUARDED_FILES.some((f) => f.endsWith(join('ds', 'Chip.css')))).toBe(true)
  // #147 M-2: the violation fixture must never reach the production sweep,
  // or the guard test below would fail on a file that exists to prove the
  // guard's detection logic, not to describe the real app.
  expect(GUARDED_FILES).not.toContain(VIOLATION_FIXTURE)
})

describe('touch-target floor cannot be beaten outside ds/Button.css or ds/IconButton.css (agent-forge-harness-hxq / pr128 F1, broadened by agent-forge-harness-opn / #138 M-1)', () => {
  it('sets no size property on .aether-btn/.aether-icon-btn, a bare button selector, or [data-size from any other stylesheet', () => {
    for (const file of GUARDED_FILES) {
      const css = stripComments(readFileSync(file, 'utf8'))
      for (const { selector, body } of blocks(css)) {
        if (!isGuardedSelector(selector)) continue
        for (const property of SIZE_PROPERTIES) {
          expect(
            new RegExp(`(^|;)\\s*${property}\\s*:`).test(body),
            `${relative(SRC_DIR, file)}: selector "${selector.trim()}" sets ${property} on a button ` +
              'element/class/attribute the ds/ 44px touch floor already governs — this can shrink the ' +
              'rendered target below 44px in the real app even though no single storybook story sees it.',
          ).toBe(false)
        }
      }
    }
  })
})

describe('positive control: the detection logic can actually find a violation (agent-forge-harness-8ug / #147 M-2)', () => {
  it('flags every guarded-selector kind and every size property on the committed fixture', () => {
    const css = stripComments(readFileSync(VIOLATION_FIXTURE, 'utf8'))
    const fixtureBlocks = blocks(css)
    expect(fixtureBlocks.length).toBeGreaterThan(0)

    const flaggedProperties = new Set<string>()
    for (const { selector, body } of fixtureBlocks) {
      expect(isGuardedSelector(selector), `fixture selector "${selector.trim()}" must be one this guard watches`).toBe(
        true,
      )
      for (const property of SIZE_PROPERTIES) {
        if (new RegExp(`(^|;)\\s*${property}\\s*:`).test(body)) flaggedProperties.add(property)
      }
    }
    // Hard-coded, not derived from SIZE_PROPERTIES: comparing against the
    // production list itself would go blind exactly when that list is the
    // thing mutated (dropping an entry from SIZE_PROPERTIES would drop it
    // from both sides of the comparison and never fail). Every property
    // below has its own isolated rule in the fixture, so a mutant that
    // empties, shrinks, or otherwise breaks the list -- or the per-property
    // regex built from it -- leaves at least one entry unflagged here, even
    // though it would never fail the real-file guard above (nothing to flag
    // either way).
    expect([...flaggedProperties].sort()).toEqual(
      [
        'min-height',
        'min-width',
        'height',
        'width',
        'min-block-size',
        'min-inline-size',
        'block-size',
        'inline-size',
      ].sort(),
    )
  })
})
