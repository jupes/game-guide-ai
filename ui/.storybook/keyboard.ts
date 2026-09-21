/**
 * Keyboard helpers for interaction stories.
 *
 * agent-forge-harness-27h, rework 1. A story called `…ByKeyboard` is making a
 * claim with two halves — the control is REACHABLE from the keyboard, and it is
 * OPERABLE from the keyboard. `element.focus()` tests only the second. It puts
 * focus on the control by script, which works just as well on a control that
 * has been taken out of the tab order entirely (`tabindex="-1"`, `display` on
 * an ancestor, a `hidden` attribute) — which is precisely the defect the first
 * half exists to rule out.
 *
 * `tabTo` walks the document with real Tab presses instead, so removing the
 * control from the tab order fails the story.
 */
import { expect, userEvent } from 'storybook/test'

/**
 * Press Tab until `target` holds focus, then assert that it does.
 *
 * `limit` bounds the walk so a control that is NOT reachable fails with the
 * story's own assertion rather than spinning: Tab cycles within the document,
 * so an unreachable target would otherwise loop forever. Raise it for a dense
 * surface; it is deliberately not `Infinity`.
 */
export async function tabTo(target: Element, limit = 20): Promise<void> {
  for (let i = 0; i < limit && document.activeElement !== target; i += 1) {
    await userEvent.tab()
  }
  await expect(target).toHaveFocus()
}
