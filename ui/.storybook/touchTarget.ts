/**
 * Per-call-site touch-target assertions for interaction stories.
 *
 * agent-forge-harness-1dw. The 44px floor (`--aether-touch-min`) lives on the
 * shared ds/Button.css and ds/IconButton.css stylesheets, and ds/Button
 * .stories.tsx + ds/IconButton.stories.tsx already assert it there (`Sizes` /
 * `DarkSizes`). But a call site can beat that floor rule with a narrower
 * selector of its own — a component-scoped class, an `!important` — without
 * either of those two ds/ stories ever noticing: the floor rule is still in
 * the CSSOM, just outranked by specificity at that one usage. Two survived
 * mutants proved exactly this (LeftNav's IconButtons, DocumentField's small
 * Buttons): every ds/ story and every jsdom test for those files stayed green
 * while the call site's rendered target shrank.
 *
 * This re-asserts the floor at the rendered call site, by accessible name, so
 * a shrink there fails the story that renders that call site — not just the
 * shared ds/ stories.
 *
 * agent-forge-harness-hxq (pr128 F1): this does NOT cover every shrinking
 * rule "a later stylesheet" could write. Each story's iframe loads only the
 * CSS its own component imports, so a rule written for `.aether-btn` /
 * `.aether-icon-btn` in a stylesheet the rendered story never imports (e.g. a
 * leak from an unrelated component, only loaded together with this one in
 * the real composed app) is invisible here too — this helper only catches a
 * shrink in CSS the story ALREADY loads. The cross-stylesheet case is
 * covered separately by the static guard in src/ds/touchTargetLeak.test.ts.
 */
import { expect, within } from 'storybook/test'

type Canvas = ReturnType<typeof within>

/** The roles a touch target is looked up by. A switch and a select are
 * controls too, and 0rn's drawer holds one of each (agent-forge-harness-0rn). */
export type TouchTargetRole = 'button' | 'switch' | 'combobox'

/** Assert one rendered control, found by its role and accessible name, clears
 * the 44x44 touch-target floor. The role defaults to `button`. */
export async function expectTouchTarget(
  canvas: Canvas,
  name: string | RegExp,
  role: TouchTargetRole = 'button',
): Promise<void> {
  const box = canvas.getByRole(role, { name }).getBoundingClientRect()
  await expect(box.width).toBeGreaterThanOrEqual(44)
  await expect(box.height).toBeGreaterThanOrEqual(44)
}

/** Assert several controls in the same canvas, each by its accessible name. */
export async function expectTouchTargets(
  canvas: Canvas,
  names: ReadonlyArray<string | RegExp>,
  role: TouchTargetRole = 'button',
): Promise<void> {
  for (const name of names) {
    await expectTouchTarget(canvas, name, role)
  }
}
