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
 * The document's scrollWidth alone is blind wherever a box clips: the ds
 * Card, the transcript, the workspace's boxes and the open drawer all hide
 * what is too wide for them before it reaches the document. So this also
 * runs expectNothingClipped over the whole page. */
export async function expectNoPageOverflow(): Promise<void> {
  const root = document.documentElement
  await expect(root.scrollWidth).toBeLessThanOrEqual(root.clientWidth)
  await expectNothingClipped(document.body)
}

/** Horizontal scrollers by design: what passes their edge is scrolled to, not
 * lost. Every other box that clips is checked. */
const INTENDED_SCROLLERS = ['.app-header__channels', '.aether-markdown pre', '.aether-markdown table'] as const

/** Controls that draw their own text: their DOM children (a select's options,
 * a textarea's value) are not laid out where they sit. */
const OPAQUE = new Set(['SELECT', 'TEXTAREA', 'SCRIPT', 'STYLE', 'TEMPLATE'])

/** Sub-pixel slack: a fractional edge against an integer clientWidth. */
const EDGE_SLACK = 0.5

/** What a node is measured against: its nearest clipping box, the viewport,
 * or nothing, inside a box that clips on purpose. */
type ClipContext = Element | 'viewport' | 'exempt'

/** Clipping on purpose: an ellipsised line, a visually hidden label (`clip`
 * or `clip-path`), or an intended scroller. */
function clipsOnPurpose(element: Element, style: CSSStyleDeclaration): boolean {
  return (
    style.textOverflow === 'ellipsis' ||
    style.clip !== 'auto' ||
    style.clipPath !== 'none' ||
    INTENDED_SCROLLERS.some((selector) => element.matches(selector))
  )
}

function describeNode(node: Node): string {
  if (!(node instanceof Element)) return `text "${(node.textContent ?? '').trim().slice(0, 32)}"`
  const classes = Array.from(node.classList, (name) => `.${name}`).join('')
  const label = node.getAttribute('aria-label')
  return `${node.tagName.toLowerCase()}${classes}${label === null ? '' : ` "${label}"`}`
}

/** What a node paints, left to right: an element's border box, or a text
 * node's line boxes. Null when it paints nothing. */
function paintedExtent(node: Node): { left: number; right: number } | null {
  if (node instanceof Element) {
    const box = node.getBoundingClientRect()
    return box.width > 0 && box.height > 0 ? box : null
  }
  if ((node.textContent ?? '').trim() === '') return null
  const range = document.createRange()
  range.selectNodeContents(node)
  const lines = Array.from(range.getClientRects()).filter((line) => line.width > 0)
  if (lines.length === 0) return null
  return { left: Math.min(...lines.map((line) => line.left)), right: Math.max(...lines.map((line) => line.right)) }
}

/**
 * WCAG 1.4.10 where expectNoPageOverflow is blind: no box on the phone hides
 * content off its side. A box clips when its `overflow-x` is not `visible`
 * (the ds Card, the transcript, the workspace's boxes, the open drawer, whose
 * `overflow-y: auto` computes `overflow-x: auto`). Every element and every
 * line of text under `root` must lie inside the padding box of its nearest
 * clipping box, on both sides; a `position: fixed` element (the drawer) is
 * measured against the viewport, since no ancestor's overflow clips it.
 * What only reaches into a box's padding is still painted, so it passes.
 * Ellipsised lines, visually hidden labels and INTENDED_SCROLLERS are exempt
 * only inside: the node's own painted box is still measured against its
 * clip context, and only its descendants are skipped. A failure names the
 * outermost node that crosses and the box it crosses, not just a number.
 */
export async function expectNothingClipped(root: Element): Promise<void> {
  await expect(await clippedNodes(root)).toEqual([])
}

/** expectNothingClipped's walk, as a list: one line per outermost node that
 * crosses its clipping box, empty when nothing does. Exported for the
 * canaries in `src/testing/NothingClipped.stories.tsx`, which pin the exact
 * lines rather than a failure message (a failed `toEqual([])` abbreviates the
 * list as "[ Array(1) ]"). */
export async function clippedNodes(root: Element): Promise<string[]> {
  await document.fonts.ready
  const childContext = new Map<Node, ClipContext>()
  const crossing = new Map<Node, ClipContext>()
  const edges = new Map<Element, readonly [number, number]>()
  const clipEdges = (context: Element | 'viewport'): readonly [number, number] => {
    if (context === 'viewport') return [0, document.documentElement.clientWidth]
    const known = edges.get(context)
    if (known !== undefined) return known
    const left = context.getBoundingClientRect().left + context.clientLeft
    const measured = [left, left + context.clientWidth] as const
    edges.set(context, measured)
    return measured
  }
  const problems: string[] = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.parentElement !== null && OPAQUE.has(node.parentElement.tagName)
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT,
  })
  for (let node: Node | null = root; node !== null; node = walker.nextNode()) {
    const parent = node === root ? null : node.parentNode
    let context = parent === null ? 'viewport' : (childContext.get(parent) ?? 'viewport')
    if (context === 'exempt') {
      childContext.set(node, 'exempt')
      continue
    }
    const element = node instanceof Element ? node : node.parentElement
    if (element === null) continue
    const style = getComputedStyle(element)
    // The exemption applies to this node's CHILDREN, not its own box: an
    // ellipsised title (or other clipsOnPurpose node) that is itself pushed
    // out of its clip context still crosses, so its own box is measured
    // below, against `context`, before `ownContext` exempts its descendants.
    let ownContext: ClipContext | undefined
    if (node === element) {
      if (style.position === 'fixed') context = 'viewport'
      ownContext = clipsOnPurpose(element, style)
        ? 'exempt'
        : style.overflowX === 'visible'
          ? context
          : element
    }
    if (style.visibility !== 'hidden') {
      const painted = paintedExtent(node)
      if (painted !== null) {
        const [left, right] = clipEdges(context)
        const pastLeft = left - painted.left
        const pastRight = painted.right - right
        if (pastLeft > EDGE_SLACK || pastRight > EDGE_SLACK) {
          crossing.set(node, context)
          // The outermost node only: what is inside it crosses with it.
          if (!(parent !== null && crossing.get(parent) === context)) {
            const sides = [
              pastLeft > EDGE_SLACK ? `${Math.round(pastLeft)}px left` : '',
              pastRight > EDGE_SLACK ? `${Math.round(pastRight)}px right` : '',
            ].filter((side) => side !== '')
            const box = context === 'viewport' ? 'the viewport' : describeNode(context)
            problems.push(`${describeNode(node)} crosses ${box} by ${sides.join(' and ')}`)
          }
        }
      }
    }
    if (ownContext !== undefined) childContext.set(node, ownContext)
  }
  return problems
}

/** The workspace's clipping boxes, outermost first. */
const WORKSPACE_CLIPS = ['.workspace-shell', '.workspace-shell__body', '.workspace-shell__main'] as const

/**
 * The workspace fits the viewport, measured where expectNoPageOverflow cannot
 * see. Each clipping box clips nothing (scrollWidth is never less than
 * clientWidth, so "clips nothing" is a difference of 0), and the composer,
 * its box and "Send message" lie inside the viewport from edge to edge. Then
 * expectNothingClipped covers every other box. Each failure names the box or
 * control that broke, not just a number.
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
  // Every other clipping box: the transcript, and the drawer when it is open.
  await expectNothingClipped(canvasElement)
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
