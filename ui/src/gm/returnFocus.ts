/**
 * returnFocus -- where focus lands when the canvas closes (CANVAS-32;
 * agent-forge-harness-1kg.6.3, C-3).
 *
 * The control that opened the canvas takes it back. A control that is gone from
 * the document, or connected but not visible (a row in a closed drawer, or in a
 * sidebar the layout has hidden: `focus()` on it is swallowed and focus would
 * fall to `<body>`), is gone, and the composer takes it instead. `checkVisibility`
 * is absent in older engines, and then only the first test applies.
 *
 * Both targets arrive as arguments: nothing here queries the document.
 */
export function returnFocus(opener: HTMLElement | null, composer: HTMLElement | null): void {
  const usable = opener !== null && opener.isConnected && opener.checkVisibility?.() !== false
  const target = usable ? opener : composer
  target?.focus()
}
