/**
 * audioStyles — a guard over the three audio stylesheets and the sources that
 * ship with them. jsdom does not evaluate CSS, so these assertions read the
 * files: they are the only place the record's presentation rules can fail
 * loudly rather than in review.
 *
 *  - LAYOUT-9  both themes, a 44px touch floor, visible focus, and every
 *              transition and animation honouring `prefers-reduced-motion`
 *  - §10.2     the narrow layout gets its own rules
 *  - the file hygiene this repository learned the hard way: not one control
 *    character anywhere in the sources this bead adds, so a stray NUL can
 *    never turn a source file into a binary blob again.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, extname, join } from 'node:path'

const GM_DIR = dirname(fileURLToPath(import.meta.url))

/** The sources this bead adds: the audio components, their styles and helpers. */
const OWNED = readdirSync(GM_DIR)
  .filter((name) => /^(Audio|audio)/.test(name))
  .filter((name) => ['.ts', '.tsx', '.css'].includes(extname(name)))
  .sort()

const STYLESHEETS = OWNED.filter((name) => name.endsWith('.css'))

const read = (name: string) => readFileSync(join(GM_DIR, name), 'utf8')
/** CSS comments hold prose about tokens; they are not declarations. */
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, ' ')

it('found the files it is meant to guard', () => {
  expect(STYLESHEETS).toEqual(['AudioConsentPrompt.css', 'AudioCue.css', 'AudioLiveStrip.css'])
  expect(OWNED.length).toBeGreaterThanOrEqual(12)
})

// ── Colour comes only from tokens, which is what makes both themes work ──────

const COLOUR_PROPERTIES = new Set([
  'color',
  'background',
  'background-color',
  'background-image',
  'border',
  'border-color',
  'border-top',
  'border-right',
  'border-bottom',
  'border-left',
  'border-block',
  'border-block-start',
  'border-block-end',
  'border-inline',
  'border-inline-start',
  'border-inline-end',
  'outline',
  'outline-color',
  'fill',
  'stroke',
  'box-shadow',
  'text-shadow',
  'accent-color',
  'caret-color',
  'text-decoration-color',
  '-webkit-tap-highlight-color',
])

/** Words that carry no colour of their own, so they need no token. */
const COLOURLESS_WORDS = new Set([
  'none',
  'transparent',
  'currentcolor',
  'inherit',
  'initial',
  'unset',
  'revert',
  'solid',
  'dashed',
  'dotted',
  'double',
  'groove',
  'ridge',
  'inset',
  'outset',
  'hidden',
  'auto',
  'in',
  'srgb',
  'oklab',
  'color-mix',
  'linear-gradient',
  'radial-gradient',
  'to',
  'at',
  'circle',
  'ellipse',
  'calc',
  'min',
  'max',
  'clamp',
  'from',
  'top',
  'bottom',
  'left',
  'right',
  'center',
])

interface Declaration {
  file: string
  property: string
  value: string
}

function declarations(): Declaration[] {
  const found: Declaration[] = []
  for (const file of STYLESHEETS) {
    const css = stripComments(read(file))
    for (const match of css.matchAll(/([-a-z]+)\s*:\s*([^;{}]+)[;}]/g)) {
      found.push({ file, property: match[1], value: match[2].trim() })
    }
  }
  return found
}

describe('colour (LAYOUT-9: both themes from the token layer)', () => {
  it('holds no hex literal and no rgb/hsl function anywhere', () => {
    for (const file of STYLESHEETS) {
      const css = stripComments(read(file))
      expect(css, `${file} must not hard-code a hex colour`).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
      expect(css, `${file} must not hard-code a colour function`).not.toMatch(
        /\b(rgba?|hsla?|hwb|lab|lch|oklch)\s*\(/i,
      )
    }
  })

  it('names a design token for every colour it paints', () => {
    for (const { file, property, value } of declarations()) {
      if (!COLOUR_PROPERTIES.has(property)) continue

      // Drop the token references, then anything numeric, and judge what is left.
      const residue = value
        .replace(/var\(\s*--[a-z0-9-]+\s*(,[^)]*)?\)/gi, ' ')
        .replace(/-?\d*\.?\d+(px|rem|em|%|s|ms|deg|fr|vw|vh|dvh)?/g, ' ')
        .replace(/[(),]/g, ' ')

      for (const word of residue.split(/\s+/).filter(Boolean)) {
        expect(
          COLOURLESS_WORDS.has(word.toLowerCase()),
          `${file}: "${property}: ${value}" paints with "${word}" rather than a token`,
        ).toBe(true)
      }
    }
  })

  it('every var() it reads is a design-system token', () => {
    for (const file of STYLESHEETS) {
      for (const match of stripComments(read(file)).matchAll(/var\(\s*(--[a-z0-9-]+)/gi)) {
        expect(match[1], `${file} reads an unknown custom property`).toMatch(/^--(md-sys|aether)-/)
      }
    }
  })

  it('defines no theme of its own — light and dark come from the tokens', () => {
    for (const file of STYLESHEETS) {
      expect(stripComments(read(file)), `${file} must not fork on the theme`).not.toMatch(
        /\[data-theme|prefers-color-scheme/,
      )
    }
  })
})

// ── Motion (LAYOUT-9, STATE-4) ───────────────────────────────────────────────

describe('reduced motion (LAYOUT-9)', () => {
  it('gives every animated stylesheet a prefers-reduced-motion block', () => {
    for (const file of STYLESHEETS) {
      const css = stripComments(read(file))
      if (!/\b(transition|animation)\s*:/.test(css)) continue
      expect(css, `${file} animates without honouring reduced motion`).toMatch(
        /@media\s*\(prefers-reduced-motion:\s*reduce\)/,
      )
    }
  })

  it('switches off each animated class by name under reduced motion', () => {
    for (const file of STYLESHEETS) {
      const css = stripComments(read(file))
      const guard = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'))
      for (const match of css.matchAll(/\.([a-z0-9_-]+)\s*\{[^}]*\b(transition|animation)\s*:/gi)) {
        const selector = match[1]
        if (/^(gm-audio)/.test(selector) === false) continue
        expect(guard, `${file}: .${selector} animates but is not stilled under reduced motion`).toContain(
          selector,
        )
      }
    }
  })

  it('keeps the live strip from claiming sound it cannot confirm (AUDIO-17)', () => {
    const css = stripComments(read('AudioLiveStrip.css'))
    expect(css).toMatch(/\[data-connection='reconnecting'\][\s\S]*?animation:\s*none/)
  })
})

// ── Touch, focus and the narrow layout ───────────────────────────────────────

describe('touch targets and focus (LAYOUT-9, §10.2)', () => {
  it('holds the 44px floor through the shared token in every stylesheet', () => {
    for (const file of STYLESHEETS) {
      expect(stripComments(read(file)), `${file} sets no touch floor`).toContain('--aether-touch-min')
    }
  })

  it('never writes a raw pixel touch floor instead of the token', () => {
    for (const { file, property, value } of declarations()) {
      if (property !== 'min-height' && property !== 'min-width') continue
      expect(value, `${file} should use --aether-touch-min for a ${property} floor`).not.toMatch(
        /\b(4[0-9]|5[0-9])px\b/,
      )
    }
  })

  it('gives every stylesheet a visible focus ring', () => {
    for (const file of STYLESHEETS) {
      const css = stripComments(read(file))
      expect(css, `${file} has no :focus-visible rule`).toContain(':focus-visible')
      const ring = css.slice(css.indexOf(':focus-visible'))
      expect(ring, `${file}'s focus ring draws no outline`).toMatch(/outline:\s*3px solid var\(--/)
    }
  })

  it('keeps the seek slider at the 44px floor (§10.2)', () => {
    const css = stripComments(read('AudioCue.css'))
    expect(css).toMatch(/\.gm-audio-cue__slider\s*\{[^}]*min-height:\s*var\(--aether-touch-min\)/)
  })

  it('answers the narrow layout from its own width, not the viewport', () => {
    for (const file of STYLESHEETS) {
      const css = stripComments(read(file))
      expect(css, `${file} has no narrow layout`).toMatch(/@container\s*\(max-width:/)
      expect(css, `${file} must declare a container to query`).toContain('container-type: inline-size')
    }
  })
})

// ── File hygiene ─────────────────────────────────────────────────────────────

/** Built rather than written, so this guard never trips over its own source. */
const REPLACEMENT_CHARACTER = String.fromCodePoint(0xfffd)
const DEFERRED_MARKERS = ['TO' + 'DO', 'FIX' + 'ME', 'HA' + 'CK']

describe('source hygiene', () => {
  it('holds no control character in any file this bead adds', () => {
    for (const file of OWNED) {
      const text = read(file)
      for (let index = 0; index < text.length; index += 1) {
        const code = text.codePointAt(index) ?? 0
        const allowed = code === 0x09 || code === 0x0a || code === 0x0d
        const control = code <= 0x1f || code === 0x7f || (code >= 0x80 && code <= 0x9f)
        expect(
          control && !allowed,
          `${file} holds control character U+${code.toString(16).padStart(4, '0')} at offset ${index}`,
        ).toBe(false)
      }
      expect(text, `${file} decoded with a replacement character — it is not clean UTF-8`).not.toContain(
        REPLACEMENT_CHARACTER,
      )
    }
  })

  it('leaves no debugging statement or deferred work behind', () => {
    const deferred = new RegExp(`\\b(${DEFERRED_MARKERS.join('|')})\\b`)
    for (const file of OWNED) {
      const text = read(file)
      expect(text, `${file} still logs`).not.toMatch(/console\.(log|debug|warn|error)\s*\(/)
      expect(text, `${file} still carries deferred work`).not.toMatch(deferred)
    }
  })
})
