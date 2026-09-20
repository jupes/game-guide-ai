/**
 * The pure half of the document renderers (agent-forge-harness-1kg.6.2).
 *
 * `GameDocument`, `DocumentField` and `SelectionBar` are views; everything they
 * decide that is arithmetic, ordering or geometry lives here, so it can be
 * tested without a rendered tree and so `react-refresh` keeps its
 * one-component-per-module rule.
 *
 * Nothing here fetches, saves or stores. Field text, document titles and
 * selections are GM-private (X-7): they pass through as arguments and come back
 * as strings, never as ids, keys, attributes, URLs or stored values.
 *
 * Two rules run through the whole module:
 *
 * - **The registry is the authority.** Order, label, kind and editability are
 *   read from `registry.ts` through its selectors — never derived from a key,
 *   and never from a type check on the document.
 * - **Lookups use `Object.hasOwn`.** `constructor` and `__proto__` both match
 *   the wire's field-key grammar (CANVAS-19), so a plain-object lookup would
 *   answer with something off a prototype instead of "not declared".
 */

import {
  ABILITY_KEYS,
  COMMON_FIELDS,
  codePointLength,
  trimWire,
  type Abilities,
  type FieldValue,
} from './contracts'
import { REGISTRY, documentTypeById } from './registry'
import { fieldLabel } from './canvasStatus'

/** CANVAS-12: a field is in exactly one of these. */
export const FIELD_STATES = ['clean', 'editing', 'saving', 'saved', 'error', 'conflict'] as const
export type FieldState = (typeof FIELD_STATES)[number]

/**
 * What the owner says about one field. The shell renders it; it never decides
 * it — saving, conflict detection and the write revision are `1kg.6.5`'s.
 */
export interface FieldStatus {
  state: FieldState
  /** `error` only (CANVAS-14): what the server said, attached to this field. */
  message?: string
  /** `conflict` only (CANVAS-20): the value that is now on the server. */
  latest?: FieldValue
}

/** One field of a document, as the shell renders it. */
export interface DocumentFieldRead {
  key: string
  /** A `FieldKind` of the contract, or something a newer server declares. */
  kind: string
  /** The registry's label. Never a key, never an HTML entity. */
  label: string
  editable: boolean
}

/** A kind's declaration, common or the type's own; `'unknown'` for neither. */
function kindOf(fields: Readonly<Record<string, string>>, key: string): string {
  if (Object.hasOwn(fields, key)) return fields[key]
  if (Object.hasOwn(COMMON_FIELDS, key)) return COMMON_FIELDS[key]
  return 'unknown'
}

/**
 * Every field of a type, in the registry's order — the common fields first,
 * then the type's own — with the registry's label, kind and `editable` flag.
 *
 * Empty for a type this bundle does not know: unknown is never NPC (X-8), and a
 * shell with no fields renders its placeholder rather than half a document.
 */
export function documentFieldReads(typeId: string): DocumentFieldRead[] {
  const type = documentTypeById(typeId)
  if (type === undefined) return []
  const keys = [...Object.keys(REGISTRY.common_field_rules), ...Object.keys(type.fields)]
  return keys.map((key) => ({
    key,
    kind: kindOf(type.fields, key),
    label: fieldLabel(key, typeId),
    // `isEditable` answers false for a key the type does not declare (X-8), so
    // this is read straight from the rule rather than defaulted here.
    editable: Object.hasOwn(type.field_rules, key)
      ? type.field_rules[key].editable
      : Object.hasOwn(REGISTRY.common_field_rules, key) && REGISTRY.common_field_rules[key].editable,
  }))
}

/** The state of one field. Absent is `clean`, and only an own key counts. */
export function statusOf(states: Readonly<Record<string, FieldStatus>>, key: string): FieldStatus {
  return Object.hasOwn(states, key) ? states[key] : { state: 'clean' }
}

/**
 * The one sentence the document's polite live region carries.
 *
 * Silent at rest, and silent while a field is being typed into or saved:
 * `Saving…` is the canvas header's aggregate (CANVAS-13) and STATE-7 rations
 * announcements to one when an operation starts and one when it ends. A
 * keystroke therefore never changes this string, which is what keeps the region
 * from reading the document back on every character.
 */
export function liveRegionMessage(
  fields: readonly DocumentFieldRead[],
  states: Readonly<Record<string, FieldStatus>>,
): string {
  const firstIn = (state: FieldState): DocumentFieldRead | undefined =>
    fields.find((field) => statusOf(states, field.key).state === state)

  const conflict = firstIn('conflict')
  if (conflict !== undefined) return `${conflict.label} changed elsewhere`
  const failed = firstIn('error')
  if (failed !== undefined) return `Couldn't save ${failed.label}`
  const saved = firstIn('saved')
  if (saved !== undefined) return `${saved.label} saved`
  return ''
}

// ── Ability scores ───────────────────────────────────────────────────────────

export type AbilityKey = (typeof ABILITY_KEYS)[number]

/** The six scores spelled out, so a screen reader never reads `str`. */
export const ABILITY_LABELS: Readonly<Record<AbilityKey, string>> = {
  str: 'Strength',
  dex: 'Dexterity',
  con: 'Constitution',
  int: 'Intelligence',
  wis: 'Wisdom',
  cha: 'Charisma',
}

/** 5e: the modifier is derived from the score, never stored beside it. */
export function abilityModifier(score: number): number {
  return Math.floor((score - 10) / 2)
}

/** `+2` / `−1`, with the typographic minus `StatBlockCard` already uses. */
export function formatModifier(modifier: number): string {
  return modifier < 0 ? `−${Math.abs(modifier)}` : `+${modifier}`
}

/** One score of an ability block, or `null` for one that was never set. */
export function abilityScore(abilities: Abilities | null | undefined, key: AbilityKey): number | null {
  if (abilities === null || abilities === undefined) return null
  return Object.hasOwn(abilities, key) ? (abilities[key] ?? null) : null
}

// ── Values ───────────────────────────────────────────────────────────────────

/** The wire's "cleared as" column. An unknown kind clears to `null`, never to
 * a guess at a shape the server would refuse. */
export function clearedValue(kind: string): FieldValue {
  switch (kind) {
    case 'text':
    case 'prose':
      return ''
    case 'text_list':
    case 'entry_list':
      return []
    default:
      return null
  }
}

function isAbilitiesRecord(value: FieldValue): value is Abilities {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Whether a field has nothing in it, so the read presentation shows its
 * placeholder. `0` is a value; an ability block of nothing but nulls is not. */
export function isBlankValue(kind: string, value: FieldValue | undefined): boolean {
  if (value === null || value === undefined) return true
  if (typeof value === 'string') return trimWire(value) === ''
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'number') return false
  if (kind === 'abilities' && isAbilitiesRecord(value)) {
    return ABILITY_KEYS.every((key) => abilityScore(value, key) === null)
  }
  return false
}

export type ValueRead<T> = { ok: true; value: T } | { ok: false; message: string }

const WHOLE_NUMBER = /^[+-]?\d+$/

/**
 * A number box read against the contract's bounds. An empty box clears the
 * field (the wire's `null`); anything that is not a whole number, or is outside
 * the bound, is a validation error that names what is allowed (STATE-2).
 */
export function readIntegerInput(raw: string, bounds: { min: number; max: number }): ValueRead<number | null> {
  const trimmed = trimWire(raw)
  if (trimmed === '') return { ok: true, value: null }
  if (!WHOLE_NUMBER.test(trimmed)) return { ok: false, message: 'Whole numbers only' }
  const value = Number(trimmed)
  if (value < bounds.min || value > bounds.max) {
    return { ok: false, message: `Between ${bounds.min} and ${bounds.max}` }
  }
  return { ok: true, value }
}

/**
 * Text read against a bound counted in CODE POINTS, as the server counts it.
 * `maxLength` on the control counts UTF-16 units, so a hundred dice would pass
 * it and still be refused on the wire; this is the check that agrees with the
 * server. The control keeps its `maxLength` as the cheap first barrier.
 */
export function readBoundedText(raw: string, max: number): ValueRead<string> {
  if (codePointLength(raw) > max) return { ok: false, message: `At most ${max} characters` }
  return { ok: true, value: raw }
}

// ── Selection geometry (CANVAS-23) ───────────────────────────────────────────

/**
 * A span of a field's text in CODE POINTS, which is what `EditScope` carries —
 * the DOM counts UTF-16 units, so a selection that starts after an emoji would
 * otherwise be sent two units too far and the server would refuse it.
 */
export function selectionOffsets(text: string, utf16Start: number, utf16End: number): { start: number; end: number } {
  const start = codePointLength(text.slice(0, utf16Start))
  return { start, end: start + codePointLength(text.slice(utf16Start, utf16End)) }
}

/**
 * The next UTF-16 index one whole character away, so a keyboard selection can
 * never cut a surrogate pair in half and send half a character to the server.
 */
export function stepCodePoint(text: string, index: number, direction: 1 | -1): number {
  if (direction === 1) {
    if (index >= text.length) return text.length
    const at = text.codePointAt(index)
    return index + (at !== undefined && at > 0xffff ? 2 : 1)
  }
  if (index <= 0) return 0
  const before = index >= 2 ? text.codePointAt(index - 2) : undefined
  return index - (before !== undefined && before > 0xffff ? 2 : 1)
}

export interface Rect {
  left: number
  top: number
  width: number
  height: number
}

export interface BarPlacement {
  /** Both are relative to the container, which is the bar's containing block. */
  left: number
  top: number
  placement: 'above' | 'below'
}

/** How far the bar keeps off the text it acts on. */
export const SELECTION_BAR_GAP = 8

/**
 * Where the bar goes: centred on the selection, never covering it, and never
 * outside the pane. It prefers to sit above; when the selection is at the top of
 * the pane it drops below rather than being clamped back over the words. The
 * horizontal clamp is what keeps it inside a 400 px canvas.
 */
export function selectionBarPosition(
  selection: Rect,
  container: Rect,
  bar: { width: number; height: number },
  gap: number = SELECTION_BAR_GAP,
): BarPlacement {
  const x = selection.left - container.left
  const y = selection.top - container.top

  const above = y - bar.height - gap
  const placement = above >= 0 ? 'above' : 'below'
  const top = placement === 'above' ? above : Math.max(0, y + selection.height + gap)

  const centred = x + selection.width / 2 - bar.width / 2
  const rightmost = Math.max(0, container.width - bar.width)
  return { left: Math.min(Math.max(0, centred), rightmost), top, placement }
}

// ── Assets (X-10, MS-7) ──────────────────────────────────────────────────────

/**
 * The one route a GM surface may load an image from: the campaign asset route
 * (MS-7). Built from two ids and nothing else — an `AssetRef` never carries a
 * URL (X-10), and both ids are escaped so that neither can climb out of the
 * path or add a query that would carry GM text out (X-7).
 */
export function assetPath(campaignId: string, assetId: string): string {
  return `/campaigns/${encodeURIComponent(campaignId)}/assets/${encodeURIComponent(assetId)}`
}
