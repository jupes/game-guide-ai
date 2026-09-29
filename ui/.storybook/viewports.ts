/**
 * Viewport helpers for phone and breakpoint stories (agent-forge-harness-0rn).
 *
 * The Storybook vitest plugin sizes the test page from a story's
 * `globals.viewport.value`, looked up in `parameters.viewport.options`. It
 * does so SILENTLY: when `@vitest/browser/context` fails to import it returns
 * without resizing, and the story runs at the 1200x900 default
 * (`@storybook/addon-vitest/dist/vitest-plugin/test-utils.js`, setViewport).
 * A phone story would then pass or fail for the wrong reason. So every story
 * that sets a viewport starts its play function with `expectViewport(name)`,
 * the canary that turns a skipped resize into a red story.
 *
 * The widths are the design's (drawn at 390, checked at 320), the ADR's (375,
 * 768, 1280), both sides of each boundary (599/600, 767/768), a folding
 * phone's 280 px cover screen, and a phone in landscape (667x375), which is
 * narrow by width but short.
 */
import { expect, within } from 'storybook/test'

const SIZES = {
  fold280: [280, 653],
  phone320: [320, 640],
  phone375: [375, 812],
  phone390: [390, 844],
  edge599: [599, 900],
  edge600: [600, 900],
  landscape667: [667, 375],
  narrow767: [767, 1024],
  wide768: [768, 1024],
  wide1280: [1280, 800],
} as const satisfies Record<string, readonly [number, number]>

export type ViewportName = keyof typeof SIZES

interface ViewportOption {
  name: string
  styles: { width: string; height: string }
}

export const VIEWPORTS: Record<ViewportName, ViewportOption> = Object.fromEntries(
  (Object.keys(SIZES) as ViewportName[]).map((name) => [
    name,
    { name, styles: { width: `${SIZES[name][0]}px`, height: `${SIZES[name][1]}px` } },
  ]),
) as Record<ViewportName, ViewportOption>

/** A story's `parameters` and `globals` for one viewport and theme. Spread it
 * into the story object; the meta's own parameters merge underneath. */
export function atViewport(name: ViewportName, theme: 'light' | 'dark' = 'light') {
  return {
    parameters: { viewport: { options: VIEWPORTS } },
    globals: { viewport: { value: name, isRotated: false }, theme },
  }
}

/** The canary: the page really is the size the story asked for.
 *
 * It also waits for the webfonts. Until Material Symbols loads, an icon's
 * ligature lays out as its word ("chat_bubble"), which is wider than the
 * glyph, so a box measured before then is the fallback font's box. Every
 * phone story measures boxes after this call. */
export async function expectViewport(name: ViewportName): Promise<void> {
  const [width, height] = SIZES[name]
  await expect({ width: window.innerWidth, height: window.innerHeight }).toEqual({ width, height })
  await document.fonts.ready
}

/** The theme canary: `atViewport(name, 'dark')` really rendered the dark
 * theme. preview.tsx sets `data-theme="dark"` for dark and removes it for
 * light, so a dropped `globals.theme` would run a "dark" story in light. */
export async function expectTheme(theme: 'light' | 'dark'): Promise<void> {
  await expect(document.documentElement.getAttribute('data-theme')).toBe(theme === 'dark' ? 'dark' : null)
}

/** WCAG 1.4.10: no page-level horizontal scroll. Internal scrollers (the
 * channel strip, a code block) are allowed; the document is not.
 *
 * Blind inside the workspace: `.workspace-shell`, its body and `<main>` are
 * all `overflow: hidden`, so anything too wide in there is clipped before it
 * reaches the document. The workspace stories add expectWorkspaceFits. */
export async function expectNoPageOverflow(): Promise<void> {
  const root = document.documentElement
  await expect(root.scrollWidth).toBeLessThanOrEqual(root.clientWidth)
}

/** The workspace's clipping boxes, outermost first. */
const WORKSPACE_CLIPS = ['.workspace-shell', '.workspace-shell__body', '.workspace-shell__main'] as const

/**
 * The workspace fits the viewport, measured where expectNoPageOverflow cannot
 * see. Each clipping box clips nothing (scrollWidth is never less than
 * clientWidth, so "clips nothing" is a difference of 0), and the composer,
 * its box and "Send message" lie inside the viewport from edge to edge. Each
 * failure names the box or control that broke, not just a number.
 */
export async function expectWorkspaceFits(canvasElement: HTMLElement): Promise<void> {
  await document.fonts.ready
  const clipped = WORKSPACE_CLIPS.map((selector) => {
    const box = canvasElement.querySelector(selector)
    if (box === null) throw new Error(`no ${selector}`)
    return { selector, clipped: box.scrollWidth - box.clientWidth }
  })
  await expect(clipped).toEqual(WORKSPACE_CLIPS.map((selector) => ({ selector, clipped: 0 })))

  const composerBox = canvasElement.querySelector('.chat-pane__composer')
  if (composerBox === null) throw new Error('no .chat-pane__composer')
  const canvas = within(canvasElement)
  const controls: ReadonlyArray<readonly [string, Element]> = [
    ['composer box', composerBox],
    ['composer', canvas.getByPlaceholderText('Ask…')],
    ['Send message', canvas.getByRole('button', { name: 'Send message' })],
  ]
  const offScreen = controls.map(([name, element]) => {
    const { left, right } = element.getBoundingClientRect()
    return { name, pastLeft: Math.max(0, -left), pastRight: Math.max(0, right - window.innerWidth) }
  })
  await expect(offScreen).toEqual(controls.map(([name]) => ({ name, pastLeft: 0, pastRight: 0 })))
}

/** One column: each element starts at or below the bottom of the one before. */
export async function expectStacked(elements: readonly Element[]): Promise<void> {
  await expect(elements.length).toBeGreaterThanOrEqual(2)
  for (let i = 1; i < elements.length; i += 1) {
    const above = elements[i - 1].getBoundingClientRect()
    const below = elements[i].getBoundingClientRect()
    await expect(below.top).toBeGreaterThanOrEqual(above.bottom - 1)
  }
}

/** Full width: the element spans its container's content box. */
export async function expectSpans(element: Element, container: Element): Promise<void> {
  const style = getComputedStyle(container)
  const content =
    container.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
  await expect(element.getBoundingClientRect().width).toBeGreaterThanOrEqual(content - 1)
}

/** The element's left edge sits at `x` CSS px, within a pixel. */
export async function expectLeftEdge(element: Element, x: number): Promise<void> {
  const left = element.getBoundingClientRect().left
  await expect(Math.abs(left - x)).toBeLessThanOrEqual(1)
}
