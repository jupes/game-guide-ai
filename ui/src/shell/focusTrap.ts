/**
 * focusTrap — LAYOUT-10's Tab wrap for a modal surface (agent-forge-harness-0rn).
 *
 * The narrow workspace drawer is modal: its background is `inert`, so Tab
 * cannot reach the page behind it, but without a wrap Tab would walk off the
 * drawer's last control into the browser chrome. This is the same wrap
 * `gm/CustomiseRailDialog.tsx` does inline; a shared Dialog primitive (N-18)
 * is where the two should meet.
 */

/** Tab stops inside a modal surface. Disabled controls are excluded: a
 * disabled ModelPicker <select> must never be the wrap target. */
export const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * For a Tab keydown inside `container`: Tab on the last stop goes to the
 * first; Shift+Tab on the first stop, or on the container itself (which holds
 * focus when the surface opens), goes to the last. Calls `preventDefault()`
 * and returns true only when it moved focus; otherwise returns false and
 * leaves the event alone. Does nothing for other keys or an empty container.
 */
export function wrapTab(
  event: Pick<KeyboardEvent, 'key' | 'shiftKey' | 'preventDefault'>,
  container: HTMLElement,
): boolean {
  if (event.key !== 'Tab') return false
  const stops = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
  if (stops.length === 0) return false
  const first = stops[0]
  const last = stops[stops.length - 1]
  const active = document.activeElement
  const target = event.shiftKey
    ? active === first || active === container
      ? last
      : null
    : active === last
      ? first
      : null
  if (target === null) return false
  event.preventDefault()
  target.focus()
  return true
}
