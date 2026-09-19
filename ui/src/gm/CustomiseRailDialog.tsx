/**
 * CustomiseRailDialog (1kg.3.3) — SLASH-15, RAIL-10, RAIL-11.
 *
 * A modal dialog listing **every** tool with a pin toggle and Move up / Move
 * down, because reordering is never drag-only (SLASH-15). Pinned tools sit
 * first, in rail order, so the order the GM is editing is the order they see.
 *
 * The rules it enforces, all of them the registry's (RAIL-11):
 *  - at most five pins, unique, in order;
 *  - a tool must be **enabled when it is pinned** (RAIL-10) — a disabled tool
 *    can still be unpinned, and a disabled pin keeps its place until then;
 *  - at five pins the remaining toggles are disabled with `Unpin one to add
 *    another`, rather than silently swallowing the tap.
 *
 * LAYOUT-10: it traps focus, closes on Escape and returns focus to its opener —
 * which is the More button, because ToolMenu hands focus back before it calls
 * `onCustomise`. Nothing is committed until Save (the working copy is local),
 * so Escape and Cancel are the same action.
 */

import * as React from 'react'
import type { ToolId } from './contracts'
import { RAIL_LIMIT, REGISTRY } from './registry'
import type { Tool, ToolAvailability } from './registry'
import { canPin, defaultPins, movePin, togglePin } from './pins'
import './CustomiseRailDialog.css'

/** SLASH-15: what a refused pin says when the rail is full. */
export const RAIL_FULL_HINT = 'Unpin one to add another'

export interface CustomiseRailDialogProps {
  open: boolean
  /** The rail's current pins. The dialog edits a copy. */
  pins: readonly ToolId[]
  availability: Readonly<Record<ToolId, ToolAvailability>>
  /** Save — the only way pins change. */
  onSave: (pins: ToolId[]) => void
  /** Cancel, Escape and the scrim all land here; nothing is committed. */
  onCancel: () => void
}

const FOCUSABLE = 'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'

function orderedTools(pins: readonly ToolId[]): Tool[] {
  const pinned = pins
    .map((id) => REGISTRY.tools.find((tool) => tool.id === id))
    .filter((tool): tool is Tool => tool !== undefined)
  return [...pinned, ...REGISTRY.tools.filter((tool) => !pins.includes(tool.id))]
}

export function CustomiseRailDialog({
  open,
  pins,
  availability,
  onSave,
  onCancel,
}: CustomiseRailDialogProps): React.JSX.Element | null {
  const id = React.useId()
  const titleId = `${id}-title`
  const dialogRef = React.useRef<HTMLDivElement>(null)
  const openerRef = React.useRef<HTMLElement | null>(null)
  // The working copy. Keyed by `open` so re-opening always starts from the
  // rail's real pins, never from an abandoned edit.
  const [draft, setDraft] = React.useState<ToolId[]>([...pins])
  const [wasOpen, setWasOpen] = React.useState(false)

  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) setDraft([...pins])
  }

  React.useEffect(() => {
    if (!open) return
    // Whoever had focus gets it back (LAYOUT-10). ToolMenu returns focus to the
    // More button before opening this, so that is what this captures.
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    // The dialog itself takes focus, so the first thing announced is its name
    // and role rather than whichever control happens to be first.
    dialogRef.current?.focus()
    const opener = openerRef.current
    return () => {
      opener?.focus()
    }
  }, [open])

  if (!open) return null

  const full = draft.length >= RAIL_LIMIT

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      onCancel()
      return
    }
    if (event.key !== 'Tab') return
    // Focus trap: a modal dialog never lets Tab reach the page behind it.
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])
    if (focusable.length === 0) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    // The dialog container counts as "before the first control": it is what
    // holds focus on open, so Shift+Tab from there must wrap to the end.
    const atStart = document.activeElement === first || document.activeElement === dialogRef.current
    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    } else if (event.shiftKey && atStart) {
      event.preventDefault()
      last.focus()
    }
  }

  return (
    <div className="gm-customise__scrim" onMouseDown={onCancel}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="gm-customise"
        onKeyDown={handleKeyDown}
        // The scrim's own mousedown closes; a press inside must not bubble to it.
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 id={titleId} className="gm-customise__title">
          Customise rail
        </h2>
        <p className="gm-customise__lede">
          {`Pin up to ${RAIL_LIMIT} tools. Everything else stays one keystroke away under More, or by typing /.`}
        </p>

        <ul className="gm-customise__list">
          {orderedTools(draft).map((tool) => {
            const pinnedAt = draft.indexOf(tool.id)
            const isPinned = pinnedAt !== -1
            const state = availability[tool.id]
            const pinnable = isPinned || canPin(draft, tool.id, availability)
            const refusal = isPinned || pinnable ? null : full ? RAIL_FULL_HINT : (state?.reason ?? 'That tool is unavailable.')
            const refusalId = refusal ? `${id}-${tool.id}-refusal` : undefined
            return (
              <li key={tool.id} className="gm-customise__row">
                <span className="material-symbols-rounded gm-customise__icon" aria-hidden="true">
                  {tool.icon}
                </span>
                <span className="gm-customise__names">
                  <span className="gm-customise__label">{tool.label}</span>
                  <span className="gm-customise__blurb">{tool.blurb}</span>
                  {refusal && (
                    <span id={refusalId} className="gm-customise__refusal">
                      {refusal}
                    </span>
                  )}
                </span>

                <span className="gm-customise__moves">
                  <button
                    type="button"
                    className="gm-customise__move"
                    aria-label={`Move ${tool.label} up`}
                    disabled={!isPinned || pinnedAt === 0}
                    onClick={() => setDraft((current) => movePin(current, tool.id, -1))}
                  >
                    <span className="material-symbols-rounded" aria-hidden="true">
                      arrow_upward
                    </span>
                  </button>
                  <button
                    type="button"
                    className="gm-customise__move"
                    aria-label={`Move ${tool.label} down`}
                    disabled={!isPinned || pinnedAt === draft.length - 1}
                    onClick={() => setDraft((current) => movePin(current, tool.id, 1))}
                  >
                    <span className="material-symbols-rounded" aria-hidden="true">
                      arrow_downward
                    </span>
                  </button>
                </span>

                {/* A switch rather than a checkbox: pinning is a setting that
                    takes effect on Save, and the DS's Switch is the control the
                    rest of the app uses for exactly that. */}
                <button
                  type="button"
                  role="switch"
                  aria-checked={isPinned}
                  aria-label={`Pin ${tool.label}`}
                  aria-disabled={!pinnable || undefined}
                  aria-describedby={refusalId}
                  data-touch-target="true"
                  className="gm-customise__pin"
                  onClick={() => {
                    if (!pinnable) return
                    setDraft((current) => togglePin(current, tool.id, availability))
                  }}
                >
                  <span className="gm-customise__pin-track">
                    <span className="gm-customise__pin-handle" />
                  </span>
                </button>
              </li>
            )
          })}
        </ul>

        <div className="gm-customise__actions">
          <button type="button" className="gm-customise__action" onClick={() => setDraft(defaultPins())}>
            Reset to defaults
          </button>
          <span className="gm-customise__spacer" />
          <button type="button" className="gm-customise__action" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="gm-customise__action gm-customise__action--primary"
            onClick={() => onSave([...draft])}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  )
}
