/**
 * breakpoints — the shell's two width boundaries (agent-forge-harness-0rn).
 *
 * Two numbers, from two sources, each an EXCLUSIVE upper bound:
 * - 600 px is the bead's phone rule (one column, 44 px targets, the meter
 *   reduced to its number). Every width media query in `ui/src/shell/*.css`
 *   is written as exactly `PHONE_MEDIA`; `breakpoints.test.ts` pins the CSS
 *   copies to this constant.
 * - 768 px is the interactions ADR's LAYOUT-3: below it the workspace has no
 *   sidebar, and LeftNav is an off-canvas drawer. The shell reads it through
 *   `useShellLayout` and keys its CSS on `data-layout`, so no stylesheet
 *   repeats it.
 *
 * Range syntax (`width < N`) reads exactly as the rules are written and leaves
 * no fractional-pixel gap, which `max-width: 599px` would at 599.5 px.
 */

import * as React from 'react'

/** Bead 0rn / design spec "Mobile": one column under 600 px (exclusive bound). */
export const PHONE_MAX_WIDTH_PX = 600
/** Interactions ADR LAYOUT-3: the narrow layout is below 768 px (exclusive bound). */
export const NARROW_MAX_WIDTH_PX = 768

/** The one string every `ui/src/shell/*.css` width media query must equal. */
export const PHONE_MEDIA = `(width < ${PHONE_MAX_WIDTH_PX}px)` as const
/** Queried by useShellLayout; never written in CSS (the shell keys on data-layout). */
export const NARROW_MEDIA = `(width < ${NARROW_MAX_WIDTH_PX}px)` as const

/** 1kg.6.3 adds 'medium' (LAYOUT-2). WorkspaceShell decides everything from
 * `satisfies Record<ShellLayout, …>` tables, so a new member is a compile
 * error at every place that must decide, not a silent fallthrough. */
export type ShellLayout = 'narrow' | 'wide'

const hasMatchMedia = (): boolean => typeof window.matchMedia === 'function'

function subscribe(onChange: () => void): () => void {
  if (!hasMatchMedia()) return () => {}
  const query = window.matchMedia(NARROW_MEDIA)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

function readLayout(): ShellLayout {
  if (!hasMatchMedia()) return 'wide'
  return window.matchMedia(NARROW_MEDIA).matches ? 'narrow' : 'wide'
}

const serverLayout = (): ShellLayout => 'wide'

/**
 * The workspace's layout, from `matchMedia(NARROW_MEDIA)`. Correct at the
 * first render (no flash of the sidebar on a phone), re-renders on the
 * query's `change` event, and removes its listener on unmount. 'wide' where
 * `window.matchMedia` is not a function (jsdom), so every test that does not
 * install a stub keeps today's layout.
 */
export function useShellLayout(): ShellLayout {
  return React.useSyncExternalStore(subscribe, readLayout, serverLayout)
}
