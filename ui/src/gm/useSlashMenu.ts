/**
 * Slash-menu state and keyboard (1kg.3.3) — record §4.3 and §4.4.
 *
 * Focus never leaves the composer: the menu is driven through
 * `aria-activedescendant` (SLASH-12), so this hook owns which option is active
 * and whether the menu is open, and the textarea keeps its native role.
 *
 * Two readings the record left implicit, both resolved toward SLASH-10 ("a
 * disabled tool can become the active option so a screen reader can reach the
 * reason"):
 *
 *  - The keyboard table's second column ("open with nothing acceptable") is
 *    about **accepting**, not about moving. Arrow / Home / End move over every
 *    listed option, disabled ones included — otherwise the active option could
 *    land on a disabled tool and the keyboard would be stuck there.
 *  - Enter and Tab fall through to the composer when the active option cannot
 *    be accepted, so Enter still submits and Tab still moves focus.
 */

import * as React from 'react'
import type { ToolId } from './contracts'
import { menuOptions } from './registry'
import type { Tool, ToolAvailability } from './registry'
import type { Parse } from './slash'

export interface SlashMenuOptions {
  /** The current parse of the draft (SLASH-6). */
  parse: Parse
  /** RAIL-10 — disabled tools are listed and reachable, but never accepted. */
  availability: Readonly<Record<ToolId, ToolAvailability>>
  /** SLASH-11: accepting completes the command. It never submits. */
  onAccept: (tool: Tool) => void
  /** SLASH-7: the menu is open only while the composer has focus. */
  focused: boolean
}

export interface SlashMenuState {
  open: boolean
  /** SLASH-9: registry order, prefix-matched on command, alias or label. */
  options: Tool[]
  /** The typed token, for the no-match row. */
  token: string
  /** `-1` when there is nothing to activate (the no-match row is inert). */
  activeIndex: number
  activeOption: Tool | null
  /** For `aria-activedescendant`; `null` while nothing is active. */
  activeOptionId: string | null
  /** For `aria-controls` and the listbox's own id. */
  listboxId: string
  optionId: (index: number) => string
  /** SLASH-12: what the polite live region should say. The caller debounces it. */
  announcement: string
  setActiveIndex: (index: number) => void
  /** A pointer pick. Refused for a disabled tool (RAIL-10). */
  pick: (tool: Tool) => void
  /** SLASH-8: Escape, a blur or an outside click. Stays closed until the token text changes. */
  dismiss: () => void
  /** Returns true when the key belonged to the menu and the composer must not act on it. */
  handleKeyDown: (event: React.KeyboardEvent) => boolean
}

function describe(tool: Tool, availability: ToolAvailability | undefined): string {
  const reason = availability?.enabled === false ? ` ${availability.reason ?? ''}`.trimEnd() : ''
  return `${tool.label}. ${tool.blurb}.${reason}`
}

export function useSlashMenu({ parse, availability, onAccept, focused }: SlashMenuOptions): SlashMenuState {
  const id = React.useId()
  const listboxId = `${id}-slash-menu`
  const optionId = React.useCallback((index: number) => `${id}-slash-option-${index}`, [id])

  const token = parse.kind === 'partial' ? parse.token : ''
  const options = React.useMemo(() => (parse.kind === 'partial' ? menuOptions(parse.token) : []), [parse])

  // SLASH-8: after Escape the menu stays closed until the token TEXT changes,
  // which is why the dismissal remembers the token rather than a boolean.
  const [dismissed, setDismissed] = React.useState<string | null>(null)
  // The active option is remembered per token, so a new token starts at the top
  // without an effect resetting state after the fact.
  const [active, setActive] = React.useState<{ token: string; index: number }>({ token: '', index: 0 })

  const open = parse.kind === 'partial' && focused && dismissed !== token
  const activeIndex = options.length === 0 ? -1 : active.token === token && active.index < options.length ? active.index : 0
  const activeOption = activeIndex === -1 ? null : (options[activeIndex] ?? null)

  const setActiveIndex = React.useCallback(
    (index: number) => {
      setActive({ token, index })
    },
    [token],
  )

  const dismiss = React.useCallback(() => {
    setDismissed(token)
  }, [token])

  const acceptable = activeOption !== null && availability[activeOption.id]?.enabled === true

  const pick = React.useCallback(
    (tool: Tool) => {
      // RAIL-10: a disabled tool cannot be armed, from any surface.
      if (availability[tool.id]?.enabled !== true) return
      onAccept(tool)
    },
    [availability, onAccept],
  )

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent): boolean => {
      // §4.4: no key is intercepted during IME composition.
      if (event.nativeEvent.isComposing) return false
      if (!open) return false
      const count = options.length
      const move = (index: number) => {
        event.preventDefault()
        setActive({ token, index })
        return true
      }
      switch (event.key) {
        case 'ArrowDown':
          return count === 0 ? false : move((activeIndex + 1) % count)
        case 'ArrowUp':
          return count === 0 ? false : move((activeIndex - 1 + count) % count)
        case 'Home':
          return count === 0 ? false : move(0)
        case 'End':
          return count === 0 ? false : move(count - 1)
        case 'Enter':
          // Shift+Enter is a newline, which closes the menu.
          if (event.shiftKey) {
            dismiss()
            return false
          }
          if (!acceptable || activeOption === null) return false
          event.preventDefault()
          onAccept(activeOption)
          return true
        case 'Tab':
          if (!acceptable || activeOption === null) return false
          event.preventDefault()
          onAccept(activeOption)
          return true
        case 'Escape':
          event.preventDefault()
          dismiss()
          return true
        default:
          return false
      }
    },
    [acceptable, activeIndex, activeOption, dismiss, onAccept, open, options.length, token],
  )

  const announcement = !open
    ? ''
    : options.length === 0
      ? `No tool matches "/${token}"`
      : `${options.length} ${options.length === 1 ? 'tool' : 'tools'}.${activeOption ? ` ${describe(activeOption, availability[activeOption.id])}` : ''}`

  return {
    open,
    options,
    token,
    activeIndex,
    activeOption,
    activeOptionId: open && activeIndex !== -1 ? optionId(activeIndex) : null,
    listboxId,
    optionId,
    announcement,
    setActiveIndex,
    pick,
    dismiss,
    handleKeyDown,
  }
}
