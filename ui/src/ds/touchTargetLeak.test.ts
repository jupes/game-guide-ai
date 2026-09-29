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
 * This is the static guard for that gap: no *.css file OUTSIDE src/ds/ (the
 * one place the 44px floor is allowed to be declared) may set min-height,
 * min-width, height or width on a selector that names `.aether-btn` or
 * `.aether-icon-btn`. jsdom does not evaluate CSS or load stylesheets
 * together the way a real page does, so — like audioStyles.test.ts — this
 * reads the files directly instead of rendering them.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'

const SRC_DIR = dirname(dirname(fileURLToPath(import.meta.url)))

/** Every *.css file under src/, outside src/ds/ (the floor's one legitimate home). */
function cssFilesOutsideDs(dir: string): string[] {
  const found: string[] = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      if (relative(SRC_DIR, full) === 'ds') continue
      found.push(...cssFilesOutsideDs(full))
    } else if (name.endsWith('.css')) {
      found.push(full)
    }
  }
  return found
}

const GUARDED_FILES = cssFilesOutsideDs(SRC_DIR)

/** CSS comments hold prose, not declarations. */
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, ' ')

/** A selector names the button classes this guard protects — not a BEM part
 * of them, such as `.aether-btn__state` or `.aether-icon-btn__label`. */
const NAMES_GUARDED_CLASS = /\.aether-(?:icon-)?btn(?![\w-])/

const SIZE_PROPERTIES = ['min-height', 'min-width', 'height', 'width']

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
  expect(GUARDED_FILES.every((f) => !relative(SRC_DIR, f).startsWith('ds' + '\\') && !relative(SRC_DIR, f).startsWith('ds/'))).toBe(true)
})

describe('touch-target floor cannot be beaten outside ds/ (agent-forge-harness-hxq / pr128 F1)', () => {
  it('sets no min-height/min-width/height/width on .aether-btn or .aether-icon-btn from any stylesheet outside src/ds/', () => {
    for (const file of GUARDED_FILES) {
      const css = stripComments(readFileSync(file, 'utf8'))
      for (const { selector, body } of blocks(css)) {
        if (!NAMES_GUARDED_CLASS.test(selector)) continue
        for (const property of SIZE_PROPERTIES) {
          expect(
            new RegExp(`(^|;)\\s*${property}\\s*:`).test(body),
            `${relative(SRC_DIR, file)}: selector "${selector.trim()}" sets ${property} on a button ` +
              'class the ds/ 44px touch floor already governs — this can shrink the rendered ' +
              'target below 44px in the real app even though no single storybook story sees it.',
          ).toBe(false)
        }
      }
    }
  })
})
