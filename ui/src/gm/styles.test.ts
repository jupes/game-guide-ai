/**
 * The rules jsdom cannot see (1kg.3.3) — LAYOUT-9, §10.2, and the DS's
 * tokens-only rule.
 *
 * jsdom evaluates no stylesheets, so these read the CSS the way
 * `ds/contrast.test.ts` reads the palette: a regression that hard-codes a
 * colour, forgets `prefers-reduced-motion` or drops the narrow layout fails
 * here rather than in review.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const FILES = ['ToolRail.css', 'ToolMenu.css', 'SlashMenu.css', 'CustomiseRailDialog.css', 'ToolComposer.css'] as const

function css(name: string): string {
  return readFileSync(join(HERE, name), 'utf-8')
}

describe('the GM surfaces take every colour from a token', () => {
  it.each(FILES)('%s has no hex colour', (name) => {
    expect(css(name).match(/#[0-9a-fA-F]{3,8}\b/g)).toBeNull()
  })

  it.each(FILES)('%s has no rgb/hsl literal', (name) => {
    expect(css(name).match(/\b(rgba?|hsla?)\(/g)).toBeNull()
  })

  it.each(FILES)('%s paints only with var() values', (name) => {
    const offenders = [...css(name).matchAll(/^\s*(background|color|border-color|border)\s*:\s*([^;]+);/gm)]
      .filter(([, , value]) => !value.includes('var(--') && !['none', 'transparent', 'inherit'].includes(value.trim()))
      .map(([, property, value]) => `${property}: ${value.trim()}`)
    expect(offenders).toEqual([])
  })
})

describe('LAYOUT-9: every transition honours prefers-reduced-motion', () => {
  it.each(FILES)('%s suppresses whatever it animates', (name) => {
    const source = css(name)
    if (!source.includes('transition:')) return
    expect(source).toContain('@media (prefers-reduced-motion: reduce)')
  })
})

describe('§10.2: the rail on medium and narrow layouts', () => {
  it('hides the `/ for all tools` hint below 900px', () => {
    expect(css('ToolRail.css')).toMatch(/@media \(max-width: 899px\)[\s\S]*?\.gm-tool-rail__hint\s*\{[\s\S]*?display: none/)
  })

  it('makes the rail one sideways-scrolling line below 768px, with More outside it', () => {
    expect(css('ToolRail.css')).toMatch(/@media \(max-width: 767px\)[\s\S]*?\.gm-tool-rail__tools\s*\{[\s\S]*?overflow-x: auto/)
  })

  it('makes Customise rail a full-screen sheet below 768px', () => {
    expect(css('CustomiseRailDialog.css')).toMatch(/@media \(max-width: 767px\)[\s\S]*?\.gm-customise\s*\{[\s\S]*?height: 100dvh/)
  })

  it('lets the slash menu take the composer width below 768px', () => {
    expect(css('SlashMenu.css')).toMatch(/@media \(max-width: 767px\)[\s\S]*?width: 100%/)
  })
})

describe('LAYOUT-9: the 44px touch floor and a visible focus ring', () => {
  it.each(['ToolRail.css', 'ToolMenu.css', 'CustomiseRailDialog.css'])('%s keeps the touch floor', (name) => {
    expect(css(name)).toContain('var(--aether-touch-min)')
  })

  it.each(['ToolRail.css', 'ToolMenu.css', 'CustomiseRailDialog.css'])('%s draws a focus ring', (name) => {
    expect(css(name)).toMatch(/:focus-visible[\s\S]*?outline: 3px solid var\(--md-sys-color-secondary\)/)
  })
})
