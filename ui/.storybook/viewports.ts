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
import { expect } from 'storybook/test'

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

/** The canary: the page really is the size the story asked for. */
export async function expectViewport(name: ViewportName): Promise<void> {
  const [width, height] = SIZES[name]
  await expect({ width: window.innerWidth, height: window.innerHeight }).toEqual({ width, height })
}

/** WCAG 1.4.10: no page-level horizontal scroll. Internal scrollers (the
 * channel strip, a code block) are allowed; the document is not. */
export async function expectNoPageOverflow(): Promise<void> {
  const root = document.documentElement
  await expect(root.scrollWidth).toBeLessThanOrEqual(root.clientWidth)
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
