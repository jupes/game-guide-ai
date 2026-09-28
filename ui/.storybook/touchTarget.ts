/**
 * Per-call-site touch-target assertions for interaction stories.
 *
 * agent-forge-harness-1dw. The 44px floor (`--aether-touch-min`) lives on the
 * shared ds/Button.css and ds/IconButton.css stylesheets, and ds/Button
 * .stories.tsx + ds/IconButton.stories.tsx already assert it there (`Sizes` /
 * `DarkSizes`). But a call site can beat that floor rule with a narrower
 * selector of its own — a component-scoped class, a later stylesheet, an
 * `!important` — without either of those two ds/ stories ever noticing: the
 * floor rule is still in the CSSOM, just outranked by specificity at that one
 * usage. Two survived mutants proved exactly this (LeftNav's IconButtons,
 * DocumentField's small Buttons): every ds/ story and every jsdom test for
 * those files stayed green while the call site's rendered target shrank.
 *
 * This re-asserts the floor at the rendered call site, by accessible name, so
 * a shrink there fails the story that renders that call site — not just the
 * shared ds/ stories.
 */
import { expect, within } from 'storybook/test'

type Canvas = ReturnType<typeof within>

/** Assert one rendered control, found by its accessible name, clears the
 * 44x44 touch-target floor. */
export async function expectTouchTarget(canvas: Canvas, name: string | RegExp): Promise<void> {
  const box = canvas.getByRole('button', { name }).getBoundingClientRect()
  await expect(box.width).toBeGreaterThanOrEqual(44)
  await expect(box.height).toBeGreaterThanOrEqual(44)
}

/** Assert several controls in the same canvas, each by its accessible name. */
export async function expectTouchTargets(
  canvas: Canvas,
  names: ReadonlyArray<string | RegExp>,
): Promise<void> {
  for (const name of names) {
    await expectTouchTarget(canvas, name)
  }
}
