/**
 * revealFields -- the safe derivation behind the reveal sheet
 * (agent-forge-harness-1kg.7.3, brief section 4 and the Critic's 2, 5, 6, 7, 10, 11).
 *
 * Everything here is pure: a registry `DocumentType`, the preview source's `data`,
 * the server's reveal picture and the seats go in; rows, a seeded draft, a mask,
 * an effect and a summary come out. Nothing here touches the network, the DOM or
 * storage.
 *
 * What may reach a player is decided in one place. A key becomes a row only if the
 * registry's allowlist AND the contract's allowlist both name it (defence in depth,
 * REVEAL-10, ED-5), so `tags`, `true_identity` and any key a type has not declared
 * can never be offered. A row is selectable only when its value is present for a
 * player (`presentForReveal`, the twin of the server's `_present`), and never for
 * an asset while the server cannot serve one (`TABLE_ASSETS_SERVED`).
 *
 * Field text, titles and aliases are GM-private (X-7): they come back as strings to
 * be rendered, never as keys, ids or stored values. The mask is KEYS only.
 */

import {
  ABILITY_KEYS,
  RESERVED_MASK_KEYS,
  presentForReveal,
  revealableFields,
  type CharacterSheetLink,
  type FieldKind,
  type RevealState,
  type Seat,
} from './contracts'
import { ABILITY_LABELS } from './documentFields'
import { REVEAL_COPY, WHO_UNKNOWN } from './revealCopy'
import {
  REGISTRY,
  defaultRevealFor,
  labelFor,
  revealGroupFor,
  revealableKeys,
  seedsOwnerDefault,
  warningFor,
  type DocumentType,
  type Registry,
} from './registry'

/**
 * Mirrors `service/reveals.py` `TABLE_ASSETS_SERVED`: nothing can mint a table
 * handle for a picture yet, so the server refuses a mask naming an asset. While it
 * is false an asset row is shown disabled and its key is never seeded or sent.
 */
export const TABLE_ASSETS_SERVED = false

export const ALIAS_FALLBACK = WHO_UNKNOWN

// ── Rows ─────────────────────────────────────────────────────────────────────

export interface RevealPreview {
  readonly key: string
  readonly label: string
  /** Plain text. Rendered as a text node and nothing else. */
  readonly text: string
}

export interface RevealRow {
  /** The group id, or the field key. */
  readonly id: string
  readonly label: string
  /** The effective keys: only the members a player could be shown. Empty when the row is not selectable. */
  readonly keys: readonly string[]
  readonly warnings: readonly string[]
  readonly preview: readonly RevealPreview[]
  readonly selectable: boolean
  readonly reason: 'empty' | 'asset' | null
}

interface StructureRow {
  readonly id: string
  readonly label: string
  /** The allowlisted members, in the group's order (or the one key). */
  readonly members: readonly string[]
  readonly grouped: boolean
}

/** The type's rows with no data: the allowlist intersection, groups folded, registry order. */
function structureOf(type: DocumentType, registry: Registry): StructureRow[] {
  const contract = revealableFields(type.id)
  const allowed = revealableKeys(type, registry).filter((key) => Object.hasOwn(contract, key))
  const allowedSet = new Set(allowed)
  const rows: StructureRow[] = []
  const folded = new Set<string>()
  for (const key of allowed) {
    const group = revealGroupFor(type, key)
    if (group === undefined) {
      rows.push({ id: key, label: labelFor(type, key, registry) ?? key, members: [key], grouped: false })
      continue
    }
    if (folded.has(group.id)) continue
    folded.add(group.id)
    rows.push({ id: group.id, label: group.label, members: group.keys.filter((member) => allowedSet.has(member)), grouped: true })
  }
  return rows
}

function valueAt(data: Readonly<Record<string, unknown>>, key: string): unknown {
  return Object.hasOwn(data, key) ? data[key] : undefined
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function previewText(kind: FieldKind, value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number') return String(value)
  if (isStringList(value)) return value.join('\n')
  if (kind === 'abilities' && typeof value === 'object' && value !== null) {
    return ABILITY_KEYS.flatMap((key) => {
      const score: unknown = Object.hasOwn(value, key) ? (value as Record<string, unknown>)[key] : undefined
      return typeof score === 'number' ? [`${ABILITY_LABELS[key]} ${score}`] : []
    }).join(' · ')
  }
  if (kind === 'entry_list' && Array.isArray(value)) {
    return value
      .flatMap((item: unknown) => {
        if (typeof item !== 'object' || item === null) return []
        const { name, text } = item as { name?: unknown; text?: unknown }
        return typeof name === 'string' && typeof text === 'string' ? [`${name}. ${text}`] : []
      })
      .join('\n')
  }
  return ''
}

/**
 * The rows the sheet offers for a document of `type` whose preview source is
 * `data`. `registry` defaults to the shipped one; a test passes a stub.
 */
export function revealRows(
  type: DocumentType,
  data: Readonly<Record<string, unknown>>,
  registry: Registry = REGISTRY,
): RevealRow[] {
  const contract = revealableFields(type.id)
  return structureOf(type, registry).map((row) => {
    const kinds = row.members.map((key) => ({ key, kind: contract[key] }))
    const live = kinds.filter(({ key, kind }) => presentForReveal(kind, valueAt(data, key)))
    const selectable = live.filter(({ kind }) => kind !== 'asset' || TABLE_ASSETS_SERVED)
    const gated = live.some(({ kind }) => kind === 'asset' && !TABLE_ASSETS_SERVED)
    const warnings = [...new Set(row.members.flatMap((key) => warningFor(type, key, registry) ?? []))]
    return {
      id: row.id,
      label: row.label,
      keys: selectable.map(({ key }) => key),
      warnings,
      preview: selectable
        .filter(({ kind }) => kind !== 'asset')
        .map(({ key, kind }) => ({ key, label: labelFor(type, key, registry) ?? key, text: previewText(kind, valueAt(data, key)) })),
      selectable: selectable.length > 0,
      reason: selectable.length > 0 ? null : gated ? 'asset' : 'empty',
    }
  })
}

// ── Seats and audiences ──────────────────────────────────────────────────────

/** Who the sheet may name. Only a confirmed seat (Critic 2): see INFERRED I-3. */
export function selectableSeats(seats: readonly Seat[]): Seat[] {
  return seats.filter((seat) => seat.status === 'confirmed')
}

/** An audience as the sheet holds it. `participants` may be empty: that is the "chosen, none ticked" state (Critic 7). */
export type DraftAudience = { readonly kind: 'table' } | { readonly kind: 'participants'; readonly ids: readonly string[] }

export function sameAudience(a: DraftAudience, b: DraftAudience): boolean {
  if (a.kind === 'table' || b.kind === 'table') return a.kind === b.kind
  return a.ids.length === b.ids.length && a.ids.every((id) => b.ids.includes(id))
}

/** What the picture says one document is showing: its audience and its one disclosure's facts. */
export interface LiveDocument {
  readonly audience: DraftAudience
  readonly type: string
  readonly version: number
  readonly mask: readonly string[]
  /** REVEAL-8: any copy shows text that differs from the latest. */
  readonly staleText: boolean
  /** Every copy is held waiting on its seat. */
  readonly allWaiting: boolean
  readonly anyWaiting: boolean
}

export function liveOf(picture: RevealState | null, documentId: string): LiveDocument | null {
  if (picture === null) return null
  const held = picture.slots.flatMap((entry) =>
    entry.live !== null && entry.live.document_id === documentId ? [{ slot: entry.slot, live: entry.live }] : [],
  )
  if (held.length === 0) return null
  const first = held[0].live
  const onTable = held.some(({ slot }) => slot.kind === 'table')
  const ids = held.flatMap(({ slot }) => (slot.kind === 'participant' ? [slot.participant_id] : []))
  return {
    audience: onTable ? { kind: 'table' } : { kind: 'participants', ids },
    type: first.type,
    version: first.version,
    mask: first.mask,
    staleText: held.some(({ live }) => live.stale_text),
    allWaiting: held.every(({ live }) => live.pending_delivery),
    anyWaiting: held.some(({ live }) => live.pending_delivery),
  }
}

export function audienceOfLive(picture: RevealState | null, documentId: string): DraftAudience | null {
  return liveOf(picture, documentId)?.audience ?? null
}

/** `Brann`, `Brann and Ana`, `Brann, Ana and 1 other`, `Brann, Ana and 2 others`. */
export function aliasList(participantIds: readonly string[], seats: readonly Seat[]): string {
  const names = participantIds.map((id) => seats.find((seat) => seat.participant_id === id)?.alias ?? ALIAS_FALLBACK)
  if (names.length <= 1) return names[0] ?? ALIAS_FALLBACK
  if (names.length === 2) return `${names[0]} and ${names[1]}`
  const others = names.length - 2
  return `${names[0]}, ${names[1]} and ${others} other${others === 1 ? '' : 's'}`
}

/** `the table`, `Brann`, or `2 players`: how an audience reads in a label. */
function audienceName(audience: DraftAudience, seats: readonly Seat[]): string {
  if (audience.kind === 'table') return REVEAL_COPY.tableName
  return audience.ids.length >= 2 ? REVEAL_COPY.playersCount(audience.ids.length) : aliasList(audience.ids, seats)
}

export interface DefaultAudience {
  readonly audience: DraftAudience
  /** The document is live to players the sheet cannot offer (Critic 6). */
  readonly partial: boolean
}

/**
 * Where the sheet opens (REVEAL-4, Critic 5 and 6). A live document opens on its own
 * audience, reduced to the seats the picker offers, and never falls back to the table:
 * a default that widens a private display to the room is a decision taken by accident.
 * A hidden character sheet opens on its linked seat only when that seat is offered.
 */
export function defaultAudience(
  type: DocumentType,
  picture: RevealState | null,
  documentId: string,
  link: CharacterSheetLink | null,
  seats: readonly Seat[],
): DefaultAudience {
  const offered = new Set(selectableSeats(seats).map((seat) => seat.participant_id))
  const live = liveOf(picture, documentId)
  if (live !== null) {
    if (live.audience.kind === 'table') return { audience: live.audience, partial: false }
    const kept = live.audience.ids.filter((id) => offered.has(id))
    return { audience: { kind: 'participants', ids: kept }, partial: kept.length !== live.audience.ids.length }
  }
  if (linkedAndOffered(type, link, offered)) return { audience: { kind: 'participants', ids: [link.participant_id] }, partial: false }
  return { audience: { kind: 'table' }, partial: false }
}

function linkedAndOffered(
  type: DocumentType,
  link: CharacterSheetLink | null,
  offered: ReadonlySet<string>,
): link is CharacterSheetLink & { participant_id: string } {
  return (
    seedsOwnerDefault(type) &&
    link !== null &&
    link.participant_id !== null &&
    link.seat_active &&
    offered.has(link.participant_id)
  )
}

// ── The draft ────────────────────────────────────────────────────────────────

/**
 * REVEAL-4, first match wins: the live mask for the audience it is live to; the
 * table's default for the table; the owner default for exactly the linked and
 * offered seat; otherwise nothing. Then reduced to the keys a row offers, and never
 * the reserved word `all`. A group live with only some of its members stays as it is
 * (the row reads ticked and its preview lists just those members), so the draft is
 * exactly what is on the table and no key is widened or lost. Never remembered
 * across opens (REVEAL-4: after a panic Stop it would re-tick the mistake).
 */
export function seedDraft(
  type: DocumentType,
  rows: readonly RevealRow[],
  audience: DraftAudience,
  live: LiveDocument | null,
  link: CharacterSheetLink | null,
  seats: readonly Seat[],
): Set<string> {
  const offered = new Set(selectableSeats(seats).map((seat) => seat.participant_id))
  let seed: readonly string[] = []
  if (live !== null && sameAudience(live.audience, audience)) seed = live.mask
  else if (audience.kind === 'table') seed = defaultRevealFor(type, 'table')
  else if (
    linkedAndOffered(type, link, offered) &&
    audience.ids.length === 1 &&
    audience.ids[0] === link.participant_id
  ) {
    seed = defaultRevealFor(type, 'owner')
  }
  const wanted = new Set(seed.filter((key) => !RESERVED_MASK_KEYS.includes(key)))
  return new Set(rows.flatMap((row) => row.keys.filter((key) => wanted.has(key))))
}

/** The keys of a row that the draft holds. A row reads ticked when this is not empty. */
export function tickedKeys(row: RevealRow, draft: ReadonlySet<string>): string[] {
  return row.keys.filter((key) => draft.has(key))
}

/** The mask to send: the draft's selectable keys, once each, in registry key order. Never `all`. */
export function maskFromDraft(type: DocumentType, rows: readonly RevealRow[], draft: ReadonlySet<string>, registry: Registry = REGISTRY): string[] {
  const offered = new Set(rows.filter((row) => row.selectable).flatMap((row) => row.keys))
  return revealableKeys(type, registry).filter(
    (key) => offered.has(key) && draft.has(key) && !RESERVED_MASK_KEYS.includes(key),
  )
}

// ── The effect (REVEAL-5, Critic 7 and 11) ───────────────────────────────────

export type RevealEffectKind = 'stop' | 'none' | 'reveal' | 'replace' | 'update' | 'move'

export interface RevealEffect {
  readonly kind: RevealEffectKind
  /** The button's name: it says what pressing it does. */
  readonly label: string
  /** Every notice that applies. They compose. */
  readonly notices: readonly string[]
}

export interface RevealEffectInput {
  readonly picture: RevealState | null
  readonly documentId: string
  readonly audience: DraftAudience
  readonly mask: readonly string[]
  readonly seats: readonly Seat[]
}

function sameKeys(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((key) => b.includes(key))
}

/** The target slots that hold another document, as ids (`null` for the table). */
function slotsHoldingOther(picture: RevealState | null, documentId: string, audience: DraftAudience): Array<string | null> {
  if (picture === null) return []
  return picture.slots.flatMap((entry) => {
    if (entry.live === null || entry.live.document_id === documentId) return []
    if (entry.slot.kind === 'table') return audience.kind === 'table' ? [null] : []
    return audience.kind === 'participants' && audience.ids.includes(entry.slot.participant_id) ? [entry.slot.participant_id] : []
  })
}

export function revealEffect({ picture, documentId, audience, mask, seats }: RevealEffectInput): RevealEffect {
  const live = liveOf(picture, documentId)
  if (live !== null && mask.length === 0) return { kind: 'stop', label: REVEAL_COPY.stopShowing, notices: [] }
  const idleLabel = live === null ? REVEAL_COPY.effectNone : REVEAL_COPY.effectUpdate
  if (audience.kind === 'participants' && audience.ids.length === 0) {
    return { kind: 'none', label: idleLabel, notices: [REVEAL_COPY.chooseAtLeastOne] }
  }
  if (mask.length === 0) return { kind: 'none', label: idleLabel, notices: [] }

  const holding = slotsHoldingOther(picture, documentId, audience)
  const replaced = holding.length === 0 ? null : holding.includes(null)
    ? REVEAL_COPY.replacesTable
    : REVEAL_COPY.replacesWho(aliasList(holding.filter((id): id is string => id !== null), seats))
  const toName = audienceName(audience, seats)

  if (live === null) {
    return replaced === null
      ? { kind: 'reveal', label: audience.kind === 'table' ? REVEAL_COPY.revealToTable : REVEAL_COPY.revealTo(toName), notices: [] }
      : { kind: 'replace', label: REVEAL_COPY.revealAndReplace, notices: [replaced] }
  }
  if (sameAudience(live.audience, audience)) {
    if (sameKeys(live.mask, mask)) return { kind: 'none', label: REVEAL_COPY.effectUpdate, notices: [REVEAL_COPY.nothingChanged] }
    return { kind: 'update', label: REVEAL_COPY.effectUpdate, notices: replaced === null ? [] : [replaced] }
  }
  const notices: string[] = []
  const stops = losing(live.audience, audience)
  if (stops !== null) notices.push(stops === 'table' ? REVEAL_COPY.stopsShowingToTable : REVEAL_COPY.stopsShowingTo(aliasList(stops, seats)))
  if (replaced !== null) notices.push(replaced)
  return {
    kind: 'move',
    label: audience.kind === 'table' ? REVEAL_COPY.moveToTable : REVEAL_COPY.moveTo(toName),
    notices,
  }
}

/** Who stops seeing it: the old audience minus the new. `null` when nobody does. */
function losing(old: DraftAudience, next: DraftAudience): 'table' | readonly string[] | null {
  if (old.kind === 'table') return next.kind === 'table' ? null : 'table'
  const lost = next.kind === 'table' ? old.ids : old.ids.filter((id) => !next.ids.includes(id))
  return lost.length === 0 ? null : lost
}

// ── The header's summary (REVEAL-13) ─────────────────────────────────────────

export interface RevealSummary {
  /** `Name & voice, Qualifier`, or `A, B, C +n` beyond three. */
  readonly fields: string
  /** `to the table`, `to Brann` or `to 2 players`. */
  readonly audience: string
  /** A copy is held until its seat is confirmed. */
  readonly waiting: boolean
}

const SUMMARY_FIELDS = 3

/**
 * What a live document is showing, for the canvas header. Group labels only when
 * every member of the group is masked, otherwise the member field labels, in row order.
 * `null` when the document is not live.
 */
export function revealSummary(
  picture: RevealState | null,
  documentId: string,
  type: DocumentType,
  seats: readonly Seat[],
  registry: Registry = REGISTRY,
): RevealSummary | null {
  const live = liveOf(picture, documentId)
  if (live === null) return null
  const masked = new Set(live.mask)
  const entries = structureOf(type, registry).flatMap((row) => {
    if (row.grouped && row.members.length > 0 && row.members.every((key) => masked.has(key))) return [row.label]
    return row.members.filter((key) => masked.has(key)).map((key) => labelFor(type, key, registry) ?? key)
  })
  const shown = entries.slice(0, SUMMARY_FIELDS).join(', ')
  const more = entries.length - SUMMARY_FIELDS
  return {
    fields: more > 0 ? `${shown} +${more}` : shown,
    audience: live.audience.kind === 'table' ? REVEAL_COPY.toTheTable : REVEAL_COPY.toWho(audienceName(live.audience, seats)),
    waiting: live.anyWaiting,
  }
}

/** The set of audience ids a draft names, for ticking seats. */
export function audienceIds(audience: DraftAudience): readonly string[] {
  return audience.kind === 'participants' ? audience.ids : []
}
