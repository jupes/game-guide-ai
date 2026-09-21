/**
 * GameDocument (agent-forge-harness-1kg.6.2) — the config-driven document shell.
 *
 * ## What it is, and what it is not
 *
 * A **controlled** component. It fetches nothing, saves nothing, stores nothing
 * and computes no reveal state: the document arrives parsed, the field states
 * arrive as props, the gold wash is a list of keys the owner passes in, and the
 * reveal badge is a string the owner read off the server's live projection
 * (REVEAL-13 — a client that decides for itself can say "nothing revealed" when
 * it simply could not confirm, which is the one thing that decision forbids).
 * Autosave, the write revision, conflicts with the server and the AI edit
 * lifecycle are `1kg.6.5`'s; the canvas shell is `1kg.6.3`'s.
 *
 * ## Config-driven
 *
 * Nothing here knows what an NPC is. Every field, in order, with its label, its
 * kind and whether it may be edited, comes from the registry; the renderer, the
 * accent, `printable` and `cites_corpus` come from the registry's selectors and
 * never from a check on the type id. A type this bundle does not know renders a
 * placeholder and never an NPC (X-8), and a *kind* it does not know still gets a
 * label and a readable value, so a newer server cannot break the page.
 *
 * The `stat_block_card` renderer lays the same fields out as a stat block: runs
 * of consecutive one-line and number fields become a stat strip, the ability
 * block its table, everything else a section. The grouping is by **kind and
 * adjacency**, so registry order is preserved exactly and a future stat-block
 * type needs no code here.
 *
 * ## CANVAS-23, the part with the sharp edges
 *
 * The bar rises only for a selection that lies inside **one** editable prose
 * field's read presentation. A selection spanning two fields raises nothing —
 * that is the rule that stops a one-line fix rewriting the dossier. The span is
 * reported in **code points**, which is what `EditScope` counts in, and with the
 * exact text the GM saw, because the server re-checks it before spending
 * anything.
 *
 * A keyboard selection raises it too, and that needs real work: a browser gives
 * no caret in ordinary text without caret browsing, so this component gives the
 * read presentation of an editable prose field a tab stop and implements the
 * selection keys itself — **Ctrl/Cmd+A** for the whole field, **Shift+Arrow** to
 * adjust one whole character at a time (never half a surrogate pair). The bar is
 * rendered *inside* its field, so Tab reaches it straight from the text, and
 * Escape dismisses it and puts focus back on the field.
 */

import * as React from 'react'

import { Button } from '../ds/Button'
import { SourceList } from '../components/SourceList'
import type { Source } from '../api'
import type { Document, FieldValue } from './contracts'
import { REGISTRY, accentToken, citesCorpus, documentTypeById, isPrintable, rendererFor } from './registry'
import {
  documentFieldReads,
  liveRegionMessage,
  selectionBarPosition,
  selectionOffsets,
  statusOf,
  stepCodePoint,
  type BarPlacement,
  type Rect,
  type DocumentFieldRead,
  type FieldStatus,
} from './documentFields'
import { DocumentField } from './DocumentField'
import { SelectionBar, type DocumentSelection, type EditAction } from './SelectionBar'
import './GameDocument.css'

/**
 * How big the bar is, for placing it before it exists. Measuring the rendered
 * bar and moving it afterwards would show the GM one frame in the wrong place
 * and cost a render per selection; these are the numbers `SelectionBar.css`
 * produces — three actions at the 44 px touch floor.
 */
const BAR_SIZE = { width: 268, height: 52 }

/** Whether a run of fields is presented as a stat strip or on its own. */
type Band = { kind: 'strip'; fields: DocumentFieldRead[] } | { kind: 'single'; field: DocumentFieldRead }

const STRIP_KINDS = new Set(['text', 'integer'])

function isCommon(key: string): boolean {
  return Object.hasOwn(REGISTRY.common_field_rules, key)
}

/**
 * Consecutive one-line and number fields of the type's own become one strip;
 * everything else stands alone. Adjacency, not a list of keys — so the registry
 * order is preserved exactly and nothing here knows what a stat block holds.
 */
function bandsOf(fields: readonly DocumentFieldRead[]): Band[] {
  const bands: Band[] = []
  let run: DocumentFieldRead[] = []
  const flush = (): void => {
    if (run.length > 1) bands.push({ kind: 'strip', fields: run })
    else for (const field of run) bands.push({ kind: 'single', field })
    run = []
  }
  for (const field of fields) {
    if (!isCommon(field.key) && STRIP_KINDS.has(field.kind)) run.push(field)
    else {
      flush()
      bands.push({ kind: 'single', field })
    }
  }
  flush()
  return bands
}

const NO_RECT: Rect = { left: 0, top: 0, width: 0, height: 0 }

/**
 * A layout box, where one can be had. jsdom implements `Range` without
 * `getBoundingClientRect`, and it could not answer honestly if it did — it
 * evaluates no stylesheets. An unmeasurable selection puts the bar at the top
 * of the pane rather than throwing; `selectionBarPosition` is unit-tested
 * against real numbers and the browser stories exercise the measured path.
 */
function rectOf(source: Element | Range): Rect {
  const measure: unknown = (source as { getBoundingClientRect?: unknown }).getBoundingClientRect
  if (typeof measure !== 'function') return NO_RECT
  const box = source.getBoundingClientRect()
  return { left: box.left, top: box.top, width: box.width, height: box.height }
}

/**
 * Whether the bar itself holds the focus.
 *
 * A bar that vanished the moment focus reached it would be unreachable from the
 * keyboard: moving focus onto a control can collapse the document selection,
 * and the bar must not be dismissed by the very act of tabbing into it. While
 * it has focus it stays; Escape or an action is what dismisses it.
 */
function barHasFocus(host: HTMLElement | null): boolean {
  const active = document.activeElement
  if (host === null || active === null) return false
  const bar = host.querySelector('.gm-selection-bar')
  return bar !== null && bar.contains(active)
}

/**
 * REVEAL-13's three readings of the badge, and the difference between two of
 * them that this component got wrong.
 *
 * An OMITTED prop is an owner who never had reveal state to pass — a fresh
 * document nobody has revealed — and `GM ONLY` is the true thing to say.
 * An EXPLICIT `null` is an owner who looked and could not confirm, which is
 * exactly the case REVEAL-13 writes out: the indicator reads `Reveal state
 * unknown — reconnecting`, **never "nothing revealed"**. `revealBadge ??
 * 'GM ONLY'` collapsed the two, so `revealBadge={projection?.badge ?? null}`
 * told the GM the table sees nothing at the moment the client stopped knowing.
 */
const REVEAL_UNKNOWN = 'Reveal state unknown — reconnecting'

function revealBadgeText(badge: string | null | undefined): string {
  if (badge === undefined) return 'GM ONLY'
  return badge === null ? REVEAL_UNKNOWN : badge
}

/** The read presentation a node sits in, or `null` for a node outside one. */
function fieldOf(node: Node | null): HTMLElement | null {
  if (node === null) return null
  const element = node.nodeType === Node.ELEMENT_NODE ? (node as HTMLElement) : node.parentElement
  return element?.closest<HTMLElement>('[data-field-read]') ?? null
}

/** `setBaseAndExtent` is not everywhere; a range is. */
function putSelection(dom: Selection, node: Node, from: number, to: number): void {
  const range = document.createRange()
  range.setStart(node, Math.min(from, to))
  range.setEnd(node, Math.max(from, to))
  dom.removeAllRanges()
  dom.addRange(range)
}

export interface GameDocumentProps {
  /** Parsed by the contract's own schema. This component never parses. */
  document: Document
  /** CANVAS-12, per field key. A key with no entry is `clean`. */
  fieldStates?: Readonly<Record<string, FieldStatus>>
  /** CANVAS-28: the keys washed gold. The parent decides; a hydrated document
   * passes none, because the wash marks a change that arrived live here. */
  changedFields?: readonly string[]
  /** CANVAS-24: the keys an AI edit is holding read-only. */
  assistantEditing?: readonly string[]
  /** REVEAL-13: the keys the table can see. Read off the server's projection. */
  revealedFields?: readonly string[]
  /**
   * REVEAL-13's badge, in the owner's words — `REVEALED`, or whatever the
   * server's live projection says. Left out, the document says `GM ONLY`,
   * which is what a document nobody has revealed is. Passed as an explicit
   * `null` — the owner looked and could not confirm — it says so, because
   * REVEAL-13 forbids reading an unconfirmed state as "nothing revealed".
   */
  revealBadge?: string | null
  /** Rendered for a `cites_corpus` type only: plain text, no links, no images. */
  sources?: readonly Source[]
  onFieldDraft?: (key: string, value: FieldValue) => void
  onFieldCommit?: (key: string, value: FieldValue) => void
  onRetry?: (key: string) => void
  onKeepMine?: (key: string, value: FieldValue) => void
  onUseLatest?: (key: string) => void
  /** CANVAS-21: arms an AI edit scoped to one field. It never runs one. */
  onArmFieldEdit?: (key: string) => void
  /** CANVAS-23. The bar reports; the owner decides whether to spend (X-1). */
  onSelectionAction?: (action: EditAction, selection: DocumentSelection) => void
  /** CANVAS-29's **Got it**. */
  onAcknowledgeChanges?: () => void
  className?: string
  style?: React.CSSProperties
}

export function GameDocument({
  document: gameDocument,
  fieldStates = {},
  changedFields = [],
  assistantEditing = [],
  revealedFields = [],
  revealBadge,
  sources = [],
  onFieldDraft,
  onFieldCommit,
  onRetry,
  onKeepMine,
  onUseLatest,
  onArmFieldEdit,
  onSelectionAction,
  onAcknowledgeChanges,
  className,
  style,
}: GameDocumentProps): React.JSX.Element {
  const rootRef = React.useRef<HTMLElement>(null)
  const [raised, setRaised] = React.useState<{ selection: DocumentSelection; placement: BarPlacement } | null>(null)

  const type = documentTypeById(gameDocument.type)
  const fields = React.useMemo(() => documentFieldReads(gameDocument.type), [gameDocument.type])

  const changed = new Set(changedFields.filter((key) => fields.some((field) => field.key === key)))
  const holding = new Set(assistantEditing)
  const revealed = new Set(revealedFields)
  const name = typeof gameDocument.data.name === 'string' ? gameDocument.data.name : ''

  /**
   * Read the live selection and decide whether it may raise the bar. Registered
   * on `selectionchange`, so it answers a pointer drag, a keyboard selection and
   * a screen reader's own selection alike — the component never asks which
   * device the GM used.
   */
  const syncSelection = React.useCallback((): void => {
    const host = rootRef.current
    const dom = window.getSelection()
    const drop = (): void => {
      if (!barHasFocus(host)) setRaised(null)
    }
    if (host === null || dom === null || dom.rangeCount === 0 || dom.isCollapsed) {
      drop()
      return
    }
    const range = dom.getRangeAt(0)
    const from = fieldOf(range.startContainer)
    // CANVAS-23: one field, or nothing. A selection that starts in `wants` and
    // ends in `leverage` raises no bar, which is what stops one action from
    // rewriting half a dossier.
    if (from === null || from !== fieldOf(range.endContainer) || !host.contains(from)) {
      drop()
      return
    }
    const key = from.getAttribute('data-field-read')
    const field = fields.find((candidate) => candidate.key === key)
    const text = range.toString()
    if (key === null || field === undefined || text === '') {
      drop()
      return
    }
    const before = document.createRange()
    before.selectNodeContents(from)
    before.setEnd(range.startContainer, range.startOffset)
    const at = before.toString().length
    const { start, end } = selectionOffsets(from.textContent ?? '', at, at + text.length)
    setRaised({
      selection: { field: key, label: field.label, start, end, text },
      placement: selectionBarPosition(rectOf(range), rectOf(host), BAR_SIZE),
    })
  }, [fields])

  React.useEffect(() => {
    document.addEventListener('selectionchange', syncSelection)
    return () => document.removeEventListener('selectionchange', syncSelection)
  }, [syncSelection])

  const dismiss = React.useCallback((): void => {
    const key = raised?.selection.field
    window.getSelection()?.removeAllRanges()
    setRaised(null)
    if (key === undefined) return
    rootRef.current?.querySelector<HTMLElement>(`[data-field-read="${key}"]`)?.focus()
  }, [raised])

  /**
   * The selection keys. A non-editable element has no caret unless the reader
   * turned caret browsing on, so `Shift+Arrow` would do nothing at all; these
   * handlers give a keyboard user the selection CANVAS-23 assumes they can make.
   */
  function onKeyDown(event: React.KeyboardEvent<HTMLElement>): void {
    const target = event.target
    if (!(target instanceof HTMLElement) || target.getAttribute('data-field-read') === null) return
    const node = target.firstChild
    const dom = window.getSelection()
    if (node === null || dom === null) return
    const whole = target.textContent ?? ''

    if (event.key === 'Escape') {
      event.preventDefault()
      dismiss()
      return
    }
    if ((event.ctrlKey || event.metaKey) && (event.key === 'a' || event.key === 'A')) {
      event.preventDefault()
      putSelection(dom, node, 0, whole.length)
      syncSelection()
      return
    }
    if (!event.shiftKey || (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft')) return
    event.preventDefault()
    const anchor = dom.anchorNode === node ? dom.anchorOffset : 0
    const focus = dom.focusNode === node ? dom.focusOffset : 0
    putSelection(dom, node, anchor, stepCodePoint(whole, focus, event.key === 'ArrowRight' ? 1 : -1))
    syncSelection()
  }

  if (type === undefined) {
    // X-8: unknown is not NPC. Nothing about this document is guessed at.
    return (
      <article className={['gm-document', className].filter(Boolean).join(' ')} data-renderer="unknown" style={style}>
        <p className="gm-document__placeholder">
          This document was made with a newer version of Aetheril. It will open once this app updates.
        </p>
      </article>
    )
  }

  function fieldNode(field: DocumentFieldRead): React.JSX.Element {
    return (
      <DocumentField
        key={field.key}
        field={field}
        value={gameDocument.data[field.key]}
        status={statusOf(fieldStates, field.key)}
        assistantEditing={holding.has(field.key)}
        changed={changed.has(field.key)}
        revealed={revealed.has(field.key)}
        documentName={name}
        campaignId={gameDocument.campaign_id}
        onDraft={onFieldDraft}
        onCommit={onFieldCommit}
        onRetry={onRetry}
        onKeepMine={onKeepMine}
        onUseLatest={onUseLatest}
        onArmEdit={onArmFieldEdit}
        selectionBar={
          raised !== null && raised.selection.field === field.key ? (
            <SelectionBar
              selection={raised.selection}
              placement={raised.placement}
              onAction={(action, selection) => onSelectionAction?.(action, selection)}
              onDismiss={dismiss}
            />
          ) : undefined
        }
      />
    )
  }

  const body =
    rendererFor(type) === 'stat_block_card'
      ? bandsOf(fields).map((band, index) =>
          band.kind === 'strip' ? (
            <div className="gm-document__stats" key={`strip-${index}`}>
              {band.fields.map(fieldNode)}
            </div>
          ) : (
            fieldNode(band.field)
          ),
        )
      : fields.map(fieldNode)

  return (
    <article
      className={['gm-document', className].filter(Boolean).join(' ')}
      ref={rootRef}
      onKeyDown={onKeyDown}
      data-renderer={rendererFor(type)}
      data-accent={accentToken(type) ?? undefined}
      data-printable={isPrintable(type) ? 'true' : undefined}
      style={style}
    >
      <header className="gm-document__head">
        <span className="gm-document__type">{type.label}</span>
        {/* REVEAL-13: repeated from the server's projection, never derived. */}
        <span className="gm-document__badge">{revealBadgeText(revealBadge)}</span>

        {/* One polite region for the whole document. It is empty at rest and a
            keystroke never changes it (CANVAS-13's aggregate is the canvas
            header's; STATE-7 rations the rest). */}
        <p className="gm-document__live" role="status" aria-live="polite">
          {liveRegionMessage(fields, fieldStates)}
        </p>

        {changed.size > 0 && onAcknowledgeChanges !== undefined && (
          <span className="gm-document__wash">
            <span className="material-symbols-rounded" aria-hidden="true">
              auto_awesome
            </span>
            New changes are highlighted
            <Button variant="text" size="small" onClick={onAcknowledgeChanges}>
              Got it
            </Button>
          </span>
        )}
      </header>

      <div className="gm-document__fields">{body}</div>

      {/* X-10: plain text and internal references only. Where a citation is
          STORED is 1kg.5.6's; this renders what the owner hands it. */}
      {citesCorpus(type) && sources.length > 0 && (
        <footer className="gm-document__sources">
          <SourceList sources={[...sources]} />
        </footer>
      )}
    </article>
  )
}
