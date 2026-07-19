/**
 * contrast — WCAG AA guard for the shell text tokens named in swe1.1
 * ('Conversations', 'Aetheril', the 'Ask…' placeholder) and the swe1.2 selected
 * conversation row.
 *
 * These color pairs are the on-<x> / <x> Material-3 roles the shell renders
 * after the token-namespace fix. The test reads the ACTUAL token values from
 * tokens/colors.css (both the :root light theme and the [data-theme="dark"]
 * tavern theme) and asserts each pair clears 4.5:1 (WCAG AA, normal text) — so a
 * future palette edit that regresses contrast fails here rather than in review.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const COLORS_CSS = join(dirname(fileURLToPath(import.meta.url)), 'tokens', 'colors.css')

type Tokens = Record<string, string>

/** Pull `--md-sys-color-*: #hex;` pairs out of a single CSS rule block. */
function parseBlock(css: string, selector: RegExp): Tokens {
  const block = css.match(selector)?.[1] ?? ''
  const tokens: Tokens = {}
  for (const m of block.matchAll(/(--md-sys-color-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})/g)) {
    tokens[m[1]] = m[2]
  }
  return tokens
}

function srgbToLinear(channel: number): number {
  const c = channel / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

function relativeLuminance(hex: string): number {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  const r = parseInt(full.slice(0, 2), 16)
  const g = parseInt(full.slice(2, 4), 16)
  const b = parseInt(full.slice(4, 6), 16)
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b)
}

function contrastRatio(fg: string, bg: string): number {
  const l1 = relativeLuminance(fg)
  const l2 = relativeLuminance(bg)
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1]
  return (hi + 0.05) / (lo + 0.05)
}

const AA_NORMAL = 4.5

// [ description, foreground role, background role ]
const PAIRS: [string, string, string][] = [
  ['Conversations title / muted text on the sidebar', '--md-sys-color-on-surface-variant', '--md-sys-color-surface-container'],
  ['conversation row text on the sidebar', '--md-sys-color-on-surface', '--md-sys-color-surface-container'],
  ["'Ask…' placeholder / tagline on the app surface", '--md-sys-color-on-surface-variant', '--md-sys-color-surface'],
  ["'Aetheril' brand + menu items on the app surface", '--md-sys-color-on-surface', '--md-sys-color-surface'],
  ['selected conversation row (swe1.2)', '--md-sys-color-on-secondary-container', '--md-sys-color-secondary-container'],
]

describe('shell text tokens meet WCAG AA (4.5:1)', () => {
  const css = readFileSync(COLORS_CSS, 'utf8')
  const themes: [string, Tokens][] = [
    ['light', parseBlock(css, /:root\s*\{([^}]*)\}/)],
    ['dark', parseBlock(css, /\[data-theme="dark"\]\s*\{([^}]*)\}/)],
  ]

  for (const [themeName, tokens] of themes) {
    for (const [label, fgRole, bgRole] of PAIRS) {
      it(`${themeName}: ${label}`, () => {
        const fg = tokens[fgRole]
        const bg = tokens[bgRole]
        expect(fg, `${fgRole} missing from ${themeName} theme`).toBeDefined()
        expect(bg, `${bgRole} missing from ${themeName} theme`).toBeDefined()
        const ratio = contrastRatio(fg, bg)
        expect(
          ratio,
          `${fgRole} on ${bgRole} = ${ratio.toFixed(2)}:1 (need ${AA_NORMAL}:1)`,
        ).toBeGreaterThanOrEqual(AA_NORMAL)
      })
    }
  }
})
