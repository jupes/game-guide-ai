/**
 * breakpoints (agent-forge-harness-0rn) — the constants, and the static guards
 * that pin the stylesheets to them.
 *
 * jsdom has no layout and no CSS, so nothing here proves a box moves; the
 * Storybook phone stories do that in Chromium. What this file owns is the
 * part a browser test cannot see: that every CSS copy of the phone boundary
 * is the SAME string as the TypeScript constant, that no shell stylesheet
 * went back to `100vh` (LAYOUT-8), and that zoom stays enabled.
 */

import { afterEach, describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { MEDIUM_MEDIA, NARROW_MEDIA, PHONE_MEDIA, useShellLayout } from './breakpoints'
import { installMatchMedia, type MatchMediaStub } from '../testing/matchMediaStub'
import { installMatchMediaWidth, type MatchMediaWidthStub } from '../testing/matchMediaWidth'

const SHELL_DIR = dirname(fileURLToPath(import.meta.url))
const SRC_DIR = dirname(SHELL_DIR)
const UI_DIR = dirname(SRC_DIR)

const SHELL_CSS = readdirSync(SHELL_DIR)
  .filter((name) => name.endsWith('.css'))
  .map((name) => ({ name, css: stripComments(readFileSync(join(SHELL_DIR, name), 'utf8')) }))

/** CSS comments hold prose, not rules (the touchTargetLeak.test.ts convention). */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, ' ')
}

const normalise = (text: string): string => text.replace(/\s+/g, ' ').trim()

/** Every `@media <condition> {` in a stylesheet, whitespace-normalised. */
function mediaConditions(css: string): string[] {
  return Array.from(css.matchAll(/@media([^{]+)\{/g), (match) => normalise(match[1]))
}

/** The body of every top-level block whose selector is exactly `selector`. */
function bodiesOf(css: string, selector: string): string[] {
  return Array.from(css.matchAll(/([^{}]+)\{([^{}]*)\}/g))
    .filter((match) => normalise(match[1]) === selector)
    .map((match) => match[2])
}

const declares = (body: string, property: string, value: string): boolean =>
  new RegExp(`(^|;)\\s*${property}\\s*:\\s*${value}\\s*(;|$)`).test(body)

describe('the breakpoint constants (T-BP-1)', () => {
  it('are the bead’s 600 px phone rule and LAYOUT-3’s 768 px narrow rule', () => {
    expect(PHONE_MEDIA).toBe('(width < 600px)')
    expect(NARROW_MEDIA).toBe('(width < 768px)')
  })

  it('add LAYOUT-1’s 1024 px medium rule (T-1)', () => {
    expect(MEDIUM_MEDIA).toBe('(width < 1024px)')
  })
})

describe('the shell stylesheets (T-BP-2, T-BP-3)', () => {
  it('scanned a real file list, so no guard below can pass on an empty set', () => {
    expect(SHELL_CSS.length).toBeGreaterThanOrEqual(10)
    expect(SHELL_CSS.map((file) => file.name)).toContain('WorkspaceShell.css')
  })

  it('writes every width media query as exactly PHONE_MEDIA', () => {
    const widthQueries = SHELL_CSS.flatMap(({ name, css }) =>
      mediaConditions(css)
        .filter((condition) => condition.includes('width'))
        .map((condition) => ({ name, condition })),
    )
    expect(widthQueries.length).toBeGreaterThanOrEqual(4)
    for (const query of widthQueries) {
      expect(query).toEqual({ name: query.name, condition: PHONE_MEDIA })
    }
  })

  it.each(['AuthScreen.css', 'Landing.css', 'ProfilePage.css', 'WorkspaceShell.css'])(
    '%s has a phone block',
    (file) => {
      const sheet = SHELL_CSS.find(({ name }) => name === file)
      expect(sheet).toBeDefined()
      expect(mediaConditions(sheet?.css ?? '')).toContain(PHONE_MEDIA)
    },
  )

  it('never sizes a screen by 100vh, which a phone’s collapsing toolbar overflows (LAYOUT-8)', () => {
    for (const { name, css } of SHELL_CSS) {
      expect({ name, has100vh: /\b100vh\b/.test(css) }).toEqual({ name, has100vh: false })
    }
  })

  it('sizes the workspace and the three card screens by 100dvh', () => {
    const sheet = (file: string): string => SHELL_CSS.find(({ name }) => name === file)?.css ?? ''
    const workspace = bodiesOf(sheet('WorkspaceShell.css'), '.workspace-shell')
    expect(workspace.some((body) => declares(body, 'height', '100dvh'))).toBe(true)
    const screens: ReadonlyArray<readonly [string, string]> = [
      ['Landing.css', '.landing'],
      ['AuthScreen.css', '.auth-screen'],
      ['ProfilePage.css', '.profile-page'],
    ]
    for (const [file, selector] of screens) {
      const bodies = bodiesOf(sheet(file), selector)
      expect({ selector, dvh: bodies.some((body) => declares(body, 'min-height', '100dvh')) }).toEqual({
        selector,
        dvh: true,
      })
    }
  })
})

describe('the canvas host stylesheet (C-16)', () => {
  it('is written for the column it is in: no width media query at all', () => {
    const css = stripComments(readFileSync(join(SRC_DIR, 'gm', 'CanvasHost.css'), 'utf8'))
    expect(css.length).toBeGreaterThan(200)
    expect(mediaConditions(css).filter((condition) => condition.includes('width'))).toEqual([])
  })
})

describe('the stat block reflows inside a 320 px chat (T-BP-4, INF-15)', () => {
  it('lets the details grid track shrink below 250 px', () => {
    const css = stripComments(readFileSync(join(SRC_DIR, 'ds', 'StatBlockCard.css'), 'utf8'))
    const [details] = bodiesOf(css, '.stat-block-card__details')
    expect(details).toContain('minmax(min(250px, 100%), 1fr)')
  })
})

describe('zoom is never disabled (T-BP-5, WCAG 1.4.4)', () => {
  it('keeps the viewport meta exactly as it was', () => {
    const html = readFileSync(join(UI_DIR, 'index.html'), 'utf8')
    const meta = /<meta\s+name="viewport"\s+content="([^"]*)"/.exec(html)
    expect(meta?.[1]).toBe('width=device-width, initial-scale=1.0')
    expect(html).not.toMatch(/maximum-scale/)
    expect(html).not.toMatch(/user-scalable/)
  })
})

describe('useShellLayout (T-HK-1..3)', () => {
  let stub: MatchMediaStub | null = null
  afterEach(() => {
    stub?.restore()
    stub = null
  })

  it('is wide where the platform has no matchMedia, so jsdom tests keep today’s layout', () => {
    expect(typeof window.matchMedia).toBe('undefined')
    const { result } = renderHook(() => useShellLayout())
    expect(result.current).toBe('wide')
  })

  it('asks NARROW_MEDIA, is narrow when it matches, and follows a change', () => {
    stub = installMatchMedia(true)
    const { result } = renderHook(() => useShellLayout())
    expect(result.current).toBe('narrow')
    expect(stub.queries).toContain(NARROW_MEDIA)
    expect(new Set(stub.queries)).toEqual(new Set([NARROW_MEDIA, MEDIUM_MEDIA]))
    act(() => stub?.set(false))
    expect(result.current).toBe('wide')
    act(() => stub?.set(true))
    expect(result.current).toBe('narrow')
  })

  it('removes its listener on unmount', () => {
    stub = installMatchMedia(false)
    const { unmount } = renderHook(() => useShellLayout())
    expect(stub.listenerCount()).toBeGreaterThan(0)
    unmount()
    expect(stub.listenerCount()).toBe(0)
  })

  it('restores the platform it found', () => {
    const own = installMatchMedia(false)
    own.restore()
    expect(typeof window.matchMedia).toBe('undefined')
  })
})

describe('useShellLayout at a width (T-1, LAYOUT-1..3)', () => {
  let stub: MatchMediaWidthStub | null = null
  afterEach(() => {
    stub?.restore()
    stub = null
  })

  it.each([
    [375, 'narrow'],
    [767, 'narrow'],
    [768, 'medium'],
    [900, 'medium'],
    [1023, 'medium'],
    [1024, 'wide'],
    [1280, 'wide'],
  ] as const)('is %i px → %s', (width, layout) => {
    stub = installMatchMediaWidth(width)
    const { result } = renderHook(() => useShellLayout())
    expect(result.current).toBe(layout)
  })

  it('follows a resize across both boundaries', () => {
    stub = installMatchMediaWidth(1280)
    const { result } = renderHook(() => useShellLayout())
    expect(result.current).toBe('wide')
    act(() => stub?.setWidth(900))
    expect(result.current).toBe('medium')
    act(() => stub?.setWidth(375))
    expect(result.current).toBe('narrow')
    act(() => stub?.setWidth(1100))
    expect(result.current).toBe('wide')
  })

  it('listens to both queries and unsubscribes from both', () => {
    stub = installMatchMediaWidth(900)
    const { unmount } = renderHook(() => useShellLayout())
    expect(new Set(stub.queries)).toEqual(new Set([NARROW_MEDIA, MEDIUM_MEDIA]))
    expect(stub.listenerCount()).toBe(2)
    unmount()
    expect(stub.listenerCount()).toBe(0)
  })
})
