/**
 * DocumentField (agent-forge-harness-1kg.6.2) — one field of a document: its
 * read presentation, its editor, and the state it is in.
 *
 * ## What it is
 *
 * A controlled view. It fetches nothing, saves nothing and stores nothing: the
 * draft lives in React state and nowhere else (CANVAS-15, X-7), every change is
 * reported through `onDraft` and every commit through `onCommit`, and autosave
 * timers, the write revision and the conversation with the server are
 * `1kg.6.5`'s.
 *
 * ## The rules it implements
 *
 * - **CANVAS-11** — an editable field is a *labelled native control*, never a
 *   bare `contentEditable`. The handoff edited every field with
 *   `contentEditable` on a `<p>`: no label, no validation, no state, and a list
 *   flattened to one comma-joined string on the way in and out. Each kind here
 *   gets the control it deserves, and a structured value is structured all the
 *   way to the callback.
 * - **CANVAS-12 to CANVAS-14, CANVAS-20** — the six states. An error attaches to
 *   its field, keeps the GM's text and offers Retry; a conflict shows both
 *   values with **Keep mine** and **Use latest**, and neither is a string: both
 *   sides render through the same view the field itself uses.
 * - **CANVAS-23** — the read presentation of an editable prose field is a tab
 *   stop that carries a selection, and the `SelectionBar` is rendered *inside*
 *   this field so that it follows the field in the tab order. Selecting text
 *   never opens the editor; only the **Edit** control does.
 * - **CANVAS-24** — while the assistant holds the field it is read-only and says
 *   `Assistant is editing…`.
 * - **CANVAS-28** — the gold wash carries a written cue as well as a colour,
 *   because colour alone fails WCAG 1.4.1.
 * - **REVEAL-13** — the eye marker is a prop. Nothing here computes reveal
 *   state; there is no `data.revealed` to read (CANVAS-33).
 * - **X-10** — an asset renders through the campaign asset route and nothing
 *   else. Field text is plain text: no Markdown, no HTML, no remote anything.
 *
 * ## Two decisions worth knowing
 *
 * **No `maxLength`.** The bound is 200 *code points*, and `maxLength` counts
 * UTF-16 units: a field holding 150 dice is 300 units, so a `maxLength` of 200
 * would refuse every keystroke in a field the server is perfectly happy with.
 * The bound is checked on commit, the way the server checks it.
 *
 * **The controls are this module's, not `ds/TextField`'s.** A field needs a
 * read-only presentation, per-field error and conflict chrome, native `min` /
 * `max`, `aria-invalid` and `aria-describedby` on the control itself, and a
 * group of controls for the structured kinds. `TextField` exposes none of that,
 * and widening it would change a component the whole app shares. The ds
 * `Button` is reused for every text action.
 */

import * as React from 'react'

import { Button } from '../ds/Button'
import {
  ABILITY_KEYS,
  ABILITY_SCORE_MAX,
  ABILITY_SCORE_MIN,
  INTEGER_FIELD_MAX,
  INTEGER_FIELD_MIN,
  LIST_FIELD_MAX_ITEMS,
  LIST_ITEM_MAX_CHARS,
  PROSE_FIELD_MAX_CHARS,
  TEXT_FIELD_MAX_CHARS,
  trimWire,
  type Abilities,
  type AssetRef,
  type Entry,
  type FieldValue,
} from './contracts'
import {
  ABILITY_LABELS,
  abilityModifier,
  abilityScore,
  assetPath,
  clearedValue,
  formatModifier,
  isBlankValue,
  readBoundedText,
  readIntegerInput,
  type AbilityKey,
  type DocumentFieldRead,
  type FieldStatus,
} from './documentFields'
import './DocumentField.css'

/** The kinds this bundle can edit. Anything else renders read-only rather than
 * sending the server a shape it was never taught. */
const EDITABLE_KINDS = new Set(['text', 'prose', 'text_list', 'integer', 'abilities', 'entry_list'])
/** The kinds whose editor is ONE control, so the label can be a real `<label>`. */
const SINGLE_CONTROL_KINDS = new Set(['text', 'prose', 'integer'])

const INTEGER_BOUNDS = { min: INTEGER_FIELD_MIN, max: INTEGER_FIELD_MAX }
const ABILITY_BOUNDS = { min: ABILITY_SCORE_MIN, max: ABILITY_SCORE_MAX }

/** Nothing has been typed into a number box yet. */
const NO_TYPING: Readonly<Record<string, string>> = {}

/**
 * One field's unsaved work, tied to the committed value it started from.
 *
 * `signature` is that value serialised. Keeping it here rather than in an
 * effect is what makes the draft self-invalidating: a new committed value has a
 * new signature, and this edit stops applying without anything having to notice.
 */
interface Edits {
  signature: string
  value: FieldValue
  /** What is literally in each number box, so a refused value stays on screen. */
  typed: Readonly<Record<string, string>>
  problem: string | null
  /**
   * Which control the refusal is about, or `null` for the whole field.
   *
   * An editor of six ability boxes has six controls and one message; without
   * this, one bad score marks all six invalid and a screen-reader user cannot
   * tell which box the message is about.
   */
  problemSlot: string | null
}

// ── Reading a bare JSON value as the kind it is ──────────────────────────────

function asText(value: FieldValue | undefined): string {
  return typeof value === 'string' ? value : ''
}

function asNumber(value: FieldValue | undefined): number | null {
  return typeof value === 'number' ? value : null
}

function asTextList(value: FieldValue | undefined): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function isEntry(item: unknown): item is Entry {
  return typeof item === 'object' && item !== null && Object.hasOwn(item, 'name') && Object.hasOwn(item, 'text')
}

function asEntryList(value: FieldValue | undefined): Entry[] {
  return Array.isArray(value) ? value.filter(isEntry) : []
}

function asAsset(value: FieldValue | undefined): AssetRef | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  return Object.hasOwn(value, 'asset_id') ? (value as AssetRef) : null
}

function asAbilities(value: FieldValue | undefined): Abilities | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  return Object.hasOwn(value, 'asset_id') ? null : (value as Abilities)
}

// ── The read presentation, shared with both sides of a conflict ──────────────

interface ValueViewProps {
  kind: string
  value: FieldValue
  label: string
  labelledBy: string
  editable: boolean
  documentName: string
  campaignId: string
  /** Set only on the prose read presentation of an editable field (CANVAS-23). */
  selectionKey?: string
  describedBy?: string
}

/**
 * How a value looks when it is not being edited. The same view renders the
 * field, the GM's side of a conflict and the server's side of one — so a
 * comparison never turns a list or an ability block into a string.
 */
function ValueView({
  kind,
  value,
  label,
  labelledBy,
  editable,
  documentName,
  campaignId,
  selectionKey,
  describedBy,
}: ValueViewProps): React.JSX.Element {
  if (isBlankValue(kind, value)) {
    return (
      <p className="gm-field__empty" role="group" aria-labelledby={labelledBy}>
        {editable ? 'Nothing yet — press Edit to add it' : 'Not set'}
      </p>
    )
  }

  if (kind === 'prose') {
    // One text node, holding exactly the field's text: the selection geometry
    // counts offsets into it, so an extra element would silently shift a span.
    return (
      <p
        className="gm-field__prose"
        role="group"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        data-field-read={selectionKey}
        tabIndex={selectionKey === undefined ? undefined : 0}
      >
        {asText(value)}
      </p>
    )
  }

  if (kind === 'text_list') {
    return (
      <ul className="gm-field__list" aria-labelledby={labelledBy}>
        {asTextList(value).map((item, index) => (
          <li key={`${index}-${item}`}>{item}</li>
        ))}
      </ul>
    )
  }

  if (kind === 'entry_list') {
    return (
      <ul className="gm-field__entries" aria-labelledby={labelledBy}>
        {asEntryList(value).map((entry, index) => (
          <li key={`${index}-${entry.name}`}>
            <p className="gm-field__entry-name">{entry.name}</p>
            <p className="gm-field__entry-text">{entry.text}</p>
          </li>
        ))}
      </ul>
    )
  }

  if (kind === 'abilities') {
    const abilities = asAbilities(value)
    return (
      <table className="gm-field__abilities" aria-labelledby={labelledBy}>
        <thead>
          <tr>
            <th scope="col">Ability</th>
            <th scope="col">Score</th>
            <th scope="col">Modifier</th>
          </tr>
        </thead>
        <tbody>
          {ABILITY_KEYS.map((key) => {
            const score = abilityScore(abilities, key)
            return (
              <tr key={key}>
                <th scope="row">{ABILITY_LABELS[key]}</th>
                {/* An absent score is absent. It is never 0, which would be a
                    real score with a real modifier. */}
                <td>{score === null ? 'Not set' : score}</td>
                <td>{score === null ? '—' : formatModifier(abilityModifier(score))}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    )
  }

  if (kind === 'asset') {
    const asset = asAsset(value)
    if (asset === null) {
      return (
        <p className="gm-field__empty" role="group" aria-labelledby={labelledBy}>
          Not set
        </p>
      )
    }
    return (
      <div className="gm-field__portrait" role="group" aria-labelledby={labelledBy}>
        <img
          // X-10, MS-7: built from two ids. An `AssetRef` never carries a URL,
          // so nothing a model could emit can become a request off this origin.
          src={assetPath(campaignId, asset.asset_id)}
          alt={trimWire(asset.alt) === '' ? `${label} of ${documentName}` : asset.alt}
          width={asset.width ?? undefined}
          height={asset.height ?? undefined}
        />
      </div>
    )
  }

  if (kind === 'integer') {
    return (
      <p className="gm-field__value" role="group" aria-labelledby={labelledBy}>
        {asNumber(value)}
      </p>
    )
  }

  if (kind === 'text') {
    return (
      <p className="gm-field__value" role="group" aria-labelledby={labelledBy}>
        {asText(value)}
      </p>
    )
  }

  // A kind from a newer server. It is labelled and it is readable when it is
  // text; it is never a raw key, and it is never editable, because this bundle
  // does not know what shape the server would accept back.
  return (
    <p className="gm-field__value" role="group" aria-labelledby={labelledBy}>
      {typeof value === 'string' || typeof value === 'number' ? value : 'Not shown in this version of the app'}
    </p>
  )
}

// ── The field ────────────────────────────────────────────────────────────────

export interface DocumentFieldProps {
  field: DocumentFieldRead
  /** The committed value from the document, or `undefined` for a missing key. */
  value: FieldValue | undefined
  /** CANVAS-12. @default { state: 'clean' } */
  status?: FieldStatus
  /** CANVAS-24: the assistant holds this field. */
  assistantEditing?: boolean
  /** CANVAS-28: this key is washed gold. */
  changed?: boolean
  /** REVEAL-13: the table can see this field right now. A prop, never derived. */
  revealed?: boolean
  /** The document's name, for an asset's alt text when the reference has none. */
  documentName: string
  /** MS-7: the campaign an asset is served under. */
  campaignId: string
  onDraft?: (key: string, value: FieldValue) => void
  onCommit?: (key: string, value: FieldValue) => void
  onRetry?: (key: string) => void
  onKeepMine?: (key: string, value: FieldValue) => void
  onUseLatest?: (key: string) => void
  /** CANVAS-21's field-scope arming gesture. It arms; it never runs. */
  onArmEdit?: (key: string) => void
  /** CANVAS-23: the bar lives here so it follows the field in the tab order. */
  selectionBar?: React.ReactNode
  className?: string
}

export function DocumentField({
  field,
  value,
  status = { state: 'clean' },
  assistantEditing = false,
  changed = false,
  revealed = false,
  documentName,
  campaignId,
  onDraft,
  onCommit,
  onRetry,
  onKeepMine,
  onUseLatest,
  onArmEdit,
  selectionBar,
  className,
}: DocumentFieldProps): React.JSX.Element {
  const ids = React.useId()
  const labelId = `${ids}-label`
  const controlId = `${ids}-control`
  const problemId = `${ids}-problem`
  const errorId = `${ids}-error`
  const hintId = `${ids}-hint`
  const conflictId = `${ids}-conflict`
  const mineId = `${ids}-mine`
  const latestId = `${ids}-latest`

  const fieldRef = React.useRef<HTMLDivElement>(null)
  const [open, setOpen] = React.useState(false)
  /** `nonce` so that asking twice for the same control focuses it twice. */
  const [focusRequest, setFocusRequest] = React.useState<{ slot: string; nonce: number }>({ slot: '', nonce: 0 })

  const committed = value === undefined ? clearedValue(field.kind) : value
  const signature = React.useMemo(() => JSON.stringify(committed ?? null), [committed])
  const [edits, setEdits] = React.useState<Edits | null>(null)
  /**
   * The draft belongs to ONE committed value. When a save lands, when `Use
   * latest` resolves a conflict, or when an AI edit changes the field, the
   * committed value — and with it the signature — changes, and the draft falls
   * away by itself. No effect and no ref: the edit is simply no longer about
   * the value in front of the GM.
   */
  const live = edits !== null && edits.signature === signature ? edits : null
  const draft = live === null ? committed : live.value
  const typed = live === null ? NO_TYPING : live.typed
  const problem = live === null ? null : live.problem
  const problemSlot = live === null ? null : live.problemSlot

  React.useEffect(() => {
    if (focusRequest.slot === '') return
    const host = fieldRef.current
    if (host === null) return
    // `editor` is "whichever control the editor put first" and `add` is the
    // one control still standing when the last row has gone; everything else
    // is a row, named by its position.
    const node =
      focusRequest.slot === 'editor'
        ? host.querySelector<HTMLElement>('.gm-field__input')
        : focusRequest.slot === 'add'
          ? host.querySelector<HTMLElement>('.gm-field__add')
          : host.querySelector<HTMLElement>(`[data-slot="${focusRequest.slot}"]`)
    node?.focus()
  }, [focusRequest])

  const editable = field.editable && !assistantEditing
  const heldOpen =
    status.state === 'editing' || status.state === 'saving' || status.state === 'error' || status.state === 'conflict'
  const editing = editable && EDITABLE_KINDS.has(field.kind) && (open || heldOpen)
  const singleControl = editing && SINGLE_CONTROL_KINDS.has(field.kind)

  /**
   * The refusal one control has to answer for.
   *
   * A refusal about the whole field reaches every control in it; a refusal
   * about one slot reaches only that slot. Without this, one ability score out
   * of range marks all six boxes invalid.
   */
  function problemFor(slot: string | null = null): string | null {
    if (problem === null) return null
    return problemSlot === null || problemSlot === slot ? problem : null
  }

  /**
   * What describes one control, as the space-separated LIST `aria-describedby`
   * actually is.
   *
   * Every editor builds its value through here and passes whatever else it
   * needs as `also`, because an editor that assigns a bare id of its own
   * REPLACES this — and a replaced description is a validation message no
   * screen reader will ever reach.
   */
  function describedByFor(slot: string | null = null, ...also: string[]): string | undefined {
    const parts = [...also, problemFor(slot) === null ? null : problemId, status.state === 'error' ? errorId : null]
      .filter((id): id is string => id !== null)
    return parts.length === 0 ? undefined : parts.join(' ')
  }

  function edit(change: Partial<Omit<Edits, 'signature'>>): void {
    setEdits((was) => {
      const base: Edits =
        was !== null && was.signature === signature
          ? was
          : { signature, value: committed, typed: NO_TYPING, problem: null, problemSlot: null }
      return { ...base, ...change, signature }
    })
  }

  function askFocus(slot: string): void {
    setFocusRequest((was) => ({ slot, nonce: was.nonce + 1 }))
  }

  /** `slot` names the one control the message is about, where there is one. */
  function refuse(message: string, slot: string | null = null): false {
    edit({ problem: message, problemSlot: slot })
    return false
  }

  /** A commit went through: the validation message goes, and the parsed value
   * replaces whatever half-typed thing produced it. */
  function settle(change: Partial<Omit<Edits, 'signature' | 'problem' | 'problemSlot'>> = {}): void {
    edit({ ...change, problem: null, problemSlot: null })
  }

  function change(next: FieldValue): void {
    edit({ value: next, problem: null, problemSlot: null })
    onDraft?.(field.key, next)
  }

  /** A structural change — add, remove, reorder — has no blur to wait for. */
  function structural(next: FieldValue, focus?: string): void {
    edit({ value: next, problem: null, problemSlot: null })
    onDraft?.(field.key, next)
    onCommit?.(field.key, next)
    if (focus !== undefined) askFocus(focus)
  }

  function cancel(): void {
    setEdits(null)
    setOpen(false)
  }

  function typedIn(slot: string, fallback: string): string {
    return Object.hasOwn(typed, slot) ? typed[slot] : fallback
  }

  // ── Commits, one per kind ──────────────────────────────────────────────────

  function commitText(): boolean {
    const max = field.kind === 'prose' ? PROSE_FIELD_MAX_CHARS : TEXT_FIELD_MAX_CHARS
    const read = readBoundedText(asText(draft), max)
    if (!read.ok) return refuse(read.message)
    settle()
    onCommit?.(field.key, read.value)
    return true
  }

  function commitInteger(): boolean {
    const current = asNumber(draft)
    const read = readIntegerInput(typedIn('', current === null ? '' : String(current)), INTEGER_BOUNDS)
    if (!read.ok) return refuse(read.message)
    settle({ value: read.value })
    onCommit?.(field.key, read.value)
    return true
  }

  function commitAbilities(): boolean {
    const current = asAbilities(draft)
    const next: Abilities = {}
    for (const key of ABILITY_KEYS) {
      const score = abilityScore(current, key)
      const read = readIntegerInput(typedIn(key, score === null ? '' : String(score)), ABILITY_BOUNDS)
      if (!read.ok) return refuse(read.message, key)
      next[key] = read.value
    }
    settle({ value: next })
    onCommit?.(field.key, next)
    return true
  }

  function commitTextList(): boolean {
    // An empty row is not an item on the wire (each one is 1 to 2,000
    // characters), so a row left blank is dropped rather than sent and refused.
    const items = asTextList(draft).filter((item) => trimWire(item) !== '')
    for (const item of items) {
      const read = readBoundedText(item, LIST_ITEM_MAX_CHARS)
      if (!read.ok) return refuse(read.message)
    }
    if (items.length > LIST_FIELD_MAX_ITEMS) return refuse(`At most ${LIST_FIELD_MAX_ITEMS} items`)
    settle()
    onCommit?.(field.key, items)
    return true
  }

  function commitEntryList(): boolean {
    // An entry with neither a name nor a text is an empty row, not an entry.
    // One with a text and no name is a mistake the GM has to see: the wire
    // needs a name of at least one character.
    const entries = asEntryList(draft).filter((entry) => trimWire(entry.name) !== '' || trimWire(entry.text) !== '')
    for (const entry of entries) {
      if (trimWire(entry.name) === '') return refuse('Every entry needs a name')
      const name = readBoundedText(entry.name, TEXT_FIELD_MAX_CHARS)
      if (!name.ok) return refuse(name.message)
      const text = readBoundedText(entry.text, LIST_ITEM_MAX_CHARS)
      if (!text.ok) return refuse(text.message)
    }
    settle()
    onCommit?.(field.key, entries)
    return true
  }

  /**
   * CANVAS-10's commit. It never closes the editor by itself: the GM may be
   * moving from one row of a list to the next, and closing on the first blur
   * would pull the control out from under them. The editor closes when focus
   * leaves the whole field, on Enter in a single-line control, or on Escape —
   * and never while the value is invalid, because that would drop typed text.
   */
  function commit(): boolean {
    switch (field.kind) {
      case 'text':
      case 'prose':
        return commitText()
      case 'integer':
        return commitInteger()
      case 'abilities':
        return commitAbilities()
      case 'text_list':
        return commitTextList()
      case 'entry_list':
        return commitEntryList()
      default:
        return true
    }
  }

  /** CANVAS-10: Ctrl/Cmd+S commits without leaving; Escape abandons the draft. */
  function onKeyDown(event: React.KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.stopPropagation()
      cancel()
      return
    }
    if ((event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S')) {
      event.preventDefault()
      commit()
      return
    }
    // A single-line control has no use for Enter, so it commits and closes. A
    // textarea keeps it, because a paragraph break is the point of prose, and a
    // list row keeps its editor open so the next row is one Tab away.
    if (event.key === 'Enter' && (field.kind === 'text' || field.kind === 'integer')) {
      event.preventDefault()
      if (commit()) setOpen(false)
    }
  }

  /**
   * One control lost focus: commit it, and close the editor only if focus has
   * left the whole field and the value was valid.
   *
   * Both halves matter. Closing on any blur would pull the editor out from
   * under a GM moving from one row of a list to the next; closing on an invalid
   * value would drop what they typed, which CANVAS-14 forbids.
   */
  function onControlBlur(event: React.FocusEvent<HTMLElement>): void {
    const committedNow = commit()
    const host = event.currentTarget.closest('.gm-field')
    const next = event.relatedTarget
    if (committedNow && (host === null || next === null || !host.contains(next))) setOpen(false)
  }

  // ── Editors ────────────────────────────────────────────────────────────────

  function textEditor(): React.JSX.Element {
    const shared = {
      id: controlId,
      className: 'gm-field__input',
      value: asText(draft),
      'aria-invalid': problemFor() !== null,
      'aria-describedby': describedByFor(),
      onKeyDown,
      onBlur: onControlBlur,
    }
    return field.kind === 'prose' ? (
      <textarea
        {...shared}
        rows={5}
        onChange={(event: React.ChangeEvent<HTMLTextAreaElement>) => change(event.target.value)}
      />
    ) : (
      <input
        {...shared}
        type="text"
        onChange={(event: React.ChangeEvent<HTMLInputElement>) => change(event.target.value)}
      />
    )
  }

  function integerEditor(): React.JSX.Element {
    const current = asNumber(draft)
    return (
      <input
        id={controlId}
        className="gm-field__input gm-field__input--number"
        type="number"
        inputMode="numeric"
        step={1}
        min={INTEGER_FIELD_MIN}
        max={INTEGER_FIELD_MAX}
        value={typedIn('', current === null ? '' : String(current))}
        aria-invalid={problemFor() !== null}
        aria-describedby={describedByFor()}
        onKeyDown={onKeyDown}
        onBlur={onControlBlur}
        onChange={(event) => {
          const raw = event.target.value
          const read = readIntegerInput(raw, INTEGER_BOUNDS)
          // A half-typed number is kept as text and says nothing yet; the
          // complaint waits for the commit, so a lone minus sign is not an
          // error the GM has to read.
          settle({ typed: { ...typed, '': raw }, ...(read.ok ? { value: read.value } : {}) })
          if (read.ok) onDraft?.(field.key, read.value)
        }}
      />
    )
  }

  function abilitiesEditor(): React.JSX.Element {
    const current = asAbilities(draft)
    return (
      <div className="gm-field__grid" role="group" aria-labelledby={labelId}>
        {ABILITY_KEYS.map((key: AbilityKey) => {
          const score = abilityScore(current, key)
          const shown = typedIn(key, score === null ? '' : String(score))
          const modifierId = `${ids}-mod-${key}`
          // The description has to describe what is IN the box. An out-of-range
          // value never enters the draft, so deriving the modifier from the
          // draft would read the PREVIOUS score's modifier back to a
          // screen-reader user while the box in front of them says 150.
          const reading = readIntegerInput(shown, ABILITY_BOUNDS)
          const modifier = !reading.ok
            ? 'Modifier unavailable'
            : reading.value === null
              ? 'Not set'
              : formatModifier(abilityModifier(reading.value))
          return (
            <div className="gm-field__score" key={key}>
              <label className="gm-field__score-label" htmlFor={`${controlId}-${key}`}>
                {ABILITY_LABELS[key]}
              </label>
              <input
                id={`${controlId}-${key}`}
                className="gm-field__input gm-field__input--number"
                type="number"
                inputMode="numeric"
                step={1}
                min={ABILITY_SCORE_MIN}
                max={ABILITY_SCORE_MAX}
                value={shown}
                aria-invalid={problemFor(key) !== null}
                // JOINED, never replaced: the modifier and, when this box is
                // the one that was refused, the refusal.
                aria-describedby={describedByFor(key, modifierId)}
                onKeyDown={onKeyDown}
                onBlur={onControlBlur}
                onChange={(event) => {
                  const raw = event.target.value
                  const read = readIntegerInput(raw, ABILITY_BOUNDS)
                  const next: Abilities = { ...(current ?? {}) }
                  if (read.ok) next[key] = read.value
                  settle({ typed: { ...typed, [key]: raw }, ...(read.ok ? { value: next } : {}) })
                  if (read.ok) onDraft?.(field.key, next)
                }}
              />
              <span className="gm-field__modifier" id={modifierId}>
                {modifier}
              </span>
            </div>
          )
        })}
      </div>
    )
  }

  function textListEditor(): React.JSX.Element {
    const items = asTextList(draft)
    const replace = (index: number, next: string): string[] => items.map((item, at) => (at === index ? next : item))
    const move = (index: number, by: number): void => {
      const next = [...items]
      const [item] = next.splice(index, 1)
      next.splice(index + by, 0, item)
      structural(next, `item-${index + by}`)
    }
    return (
      <div className="gm-field__rows" role="group" aria-labelledby={labelId}>
        {items.map((item, index) => (
          <div className="gm-field__row" key={`row-${index}`}>
            <input
              type="text"
              className="gm-field__input"
              aria-label={`${field.label} ${index + 1}`}
              aria-invalid={problemFor() !== null}
              aria-describedby={describedByFor()}
              data-slot={`item-${index}`}
              value={item}
              onKeyDown={onKeyDown}
              onBlur={onControlBlur}
              onChange={(event) => change(replace(index, event.target.value))}
            />
            {rowControls(index, items.length, move, () => {
              const next = items.filter((_, at) => at !== index)
              // Removing the LAST row leaves no row to land on, and focus
              // would fall to `<body>` — a keyboard user loses their place in
              // the document and a screen reader stops reading the field.
              structural(next, next.length === 0 ? 'add' : `item-${Math.max(0, index - 1)}`)
            })}
          </div>
        ))}
        <Button
          variant="text"
          size="small"
          icon="add"
          className="gm-field__add"
          onClick={() => {
            const next = [...items, '']
            settle({ value: next })
            onDraft?.(field.key, next)
            askFocus(`item-${next.length - 1}`)
          }}
        >
          {`Add to ${field.label}`}
        </Button>
      </div>
    )
  }

  function entryListEditor(): React.JSX.Element {
    const entries = asEntryList(draft)
    const replace = (index: number, next: Entry): Entry[] => entries.map((entry, at) => (at === index ? next : entry))
    const move = (index: number, by: number): void => {
      const next = [...entries]
      const [entry] = next.splice(index, 1)
      next.splice(index + by, 0, entry)
      structural(next, `item-${index + by}`)
    }
    return (
      <div className="gm-field__rows" role="group" aria-labelledby={labelId}>
        {entries.map((entry, index) => (
          <div className="gm-field__entry-row" key={`entry-${index}`}>
            <input
              type="text"
              className="gm-field__input"
              aria-label={`${field.label} ${index + 1} name`}
              aria-invalid={problemFor() !== null}
              aria-describedby={describedByFor()}
              data-slot={`item-${index}`}
              value={entry.name}
              onKeyDown={onKeyDown}
              onBlur={onControlBlur}
              onChange={(event) => change(replace(index, { ...entry, name: event.target.value }))}
            />
            <textarea
              className="gm-field__input"
              aria-label={`${field.label} ${index + 1} text`}
              aria-invalid={problemFor() !== null}
              aria-describedby={describedByFor()}
              rows={3}
              value={entry.text}
              onKeyDown={onKeyDown}
              onBlur={onControlBlur}
              onChange={(event) => change(replace(index, { ...entry, text: event.target.value }))}
            />
            {rowControls(index, entries.length, move, () => {
              const next = entries.filter((_, at) => at !== index)
              structural(next, next.length === 0 ? 'add' : `item-${Math.max(0, index - 1)}`)
            })}
          </div>
        ))}
        <Button
          variant="text"
          size="small"
          icon="add"
          className="gm-field__add"
          onClick={() => {
            const next = [...entries, { name: '', text: '' }]
            settle({ value: next })
            onDraft?.(field.key, next)
            askFocus(`item-${next.length - 1}`)
          }}
        >
          {`Add to ${field.label}`}
        </Button>
      </div>
    )
  }

  function rowControls(
    index: number,
    length: number,
    move: (index: number, by: number) => void,
    remove: () => void,
  ): React.JSX.Element {
    return (
      <span className="gm-field__row-actions">
        <button
          type="button"
          className="gm-field__icon"
          aria-label={`Move ${field.label} ${index + 1} up`}
          disabled={index === 0}
          onClick={() => move(index, -1)}
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            arrow_upward
          </span>
        </button>
        <button
          type="button"
          className="gm-field__icon"
          aria-label={`Move ${field.label} ${index + 1} down`}
          disabled={index === length - 1}
          onClick={() => move(index, 1)}
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            arrow_downward
          </span>
        </button>
        <button
          type="button"
          className="gm-field__icon"
          aria-label={`Remove ${field.label} ${index + 1}`}
          onClick={remove}
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            close
          </span>
        </button>
      </span>
    )
  }

  function editor(): React.JSX.Element {
    switch (field.kind) {
      case 'prose':
      case 'text':
        return textEditor()
      case 'integer':
        return integerEditor()
      case 'abilities':
        return abilitiesEditor()
      case 'text_list':
        return textListEditor()
      default:
        return entryListEditor()
    }
  }

  function view(shown: FieldValue, labelledBy: string, selectable: boolean): React.JSX.Element {
    return (
      <ValueView
        kind={field.kind}
        value={shown}
        label={field.label}
        labelledBy={labelledBy}
        editable={field.editable}
        documentName={documentName}
        campaignId={campaignId}
        selectionKey={selectable ? field.key : undefined}
        describedBy={selectable ? hintId : undefined}
      />
    )
  }

  const asset = asAsset(committed)
  const stateNote = status.state === 'saving' ? 'Saving…' : status.state === 'saved' ? 'Saved' : null
  const selectable = field.kind === 'prose' && editable && !isBlankValue(field.kind, committed)

  return (
    <div
      className={['gm-field', className].filter(Boolean).join(' ')}
      ref={fieldRef}
      data-kind={field.kind}
      data-state={status.state}
      data-changed={changed ? 'true' : undefined}
      data-assistant={assistantEditing ? 'true' : undefined}
    >
      <div className="gm-field__head">
        {singleControl ? (
          <label className="gm-field__label" id={labelId} htmlFor={controlId}>
            {field.label}
          </label>
        ) : (
          <span className="gm-field__label" id={labelId}>
            {field.label}
          </span>
        )}

        {/* CANVAS-28: colour alone fails WCAG 1.4.1, so the wash is written out. */}
        {changed && (
          <span className="gm-field__flag" data-flag="changed">
            <span className="material-symbols-rounded" aria-hidden="true">
              auto_awesome
            </span>
            Changed
          </span>
        )}
        {revealed && (
          <span className="gm-field__flag" data-flag="revealed">
            <span className="material-symbols-rounded" aria-hidden="true">
              visibility
            </span>
            The table can see this
          </span>
        )}
        {assistantEditing && (
          <span className="gm-field__flag" data-flag="assistant">
            Assistant is editing…
          </span>
        )}
        {stateNote !== null && (
          <span className="gm-field__flag" data-flag="state">
            {stateNote}
          </span>
        )}

        <span className="gm-field__actions">
          {editable && EDITABLE_KINDS.has(field.kind) && !editing && (
            <button
              type="button"
              className="gm-field__icon"
              aria-label={`Edit ${field.label}`}
              onClick={() => {
                setOpen(true)
                askFocus('editor')
              }}
            >
              <span className="material-symbols-rounded" aria-hidden="true">
                edit
              </span>
            </button>
          )}
          {editable && field.kind === 'asset' && asset !== null && (
            <button
              type="button"
              className="gm-field__icon"
              aria-label={`Remove ${field.label}`}
              onClick={() => onCommit?.(field.key, null)}
            >
              <span className="material-symbols-rounded" aria-hidden="true">
                close
              </span>
            </button>
          )}
          {editable && onArmEdit !== undefined && (
            <button
              type="button"
              className="gm-field__icon"
              aria-label={`Edit ${field.label} with assistant`}
              onClick={() => onArmEdit(field.key)}
            >
              <span className="material-symbols-rounded" aria-hidden="true">
                auto_fix_high
              </span>
            </button>
          )}
        </span>
      </div>

      {editing ? editor() : view(committed, labelId, selectable)}

      {selectable && (
        <span className="gm-field__hint" id={hintId}>
          Select any of this text to rewrite it with the assistant. Press Control A to select all of it.
        </span>
      )}

      {problem !== null && (
        <p className="gm-field__problem" id={problemId}>
          {problem}
        </p>
      )}

      {/* CANVAS-14 + STATE-2: the failure is attached to its field, the typed
          text is still there, and the next action is named. */}
      {status.state === 'error' && (
        <p className="gm-field__problem" id={errorId}>
          {status.message ?? "Couldn't save"}
          {onRetry !== undefined && (
            <button
              type="button"
              className="gm-field__text-button"
              aria-label={`Retry saving ${field.label}`}
              onClick={() => onRetry(field.key)}
            >
              Retry
            </button>
          )}
        </p>
      )}

      {/* CANVAS-20: both values, and the local text survives until the GM picks. */}
      {status.state === 'conflict' && (
        <div className="gm-field__conflict" role="group" aria-labelledby={conflictId}>
          <p className="gm-field__conflict-head" id={conflictId}>{`${field.label} changed elsewhere`}</p>
          <div className="gm-field__conflict-side">
            <p className="gm-field__conflict-label" id={mineId}>
              Mine
            </p>
            {view(draft, mineId, false)}
            <Button variant="tonal" size="small" onClick={() => onKeepMine?.(field.key, draft)}>
              Keep mine
            </Button>
          </div>
          <div className="gm-field__conflict-side">
            <p className="gm-field__conflict-label" id={latestId}>
              Latest
            </p>
            {view(status.latest === undefined ? clearedValue(field.kind) : status.latest, latestId, false)}
            <Button variant="outlined" size="small" onClick={() => onUseLatest?.(field.key)}>
              Use latest
            </Button>
          </div>
        </div>
      )}

      {selectionBar}
    </div>
  )
}
