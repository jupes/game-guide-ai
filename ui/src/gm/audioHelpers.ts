/**
 * audioHelpers — the pure parts of the GM audio surfaces (`1kg.8.4`).
 *
 * Everything here is a total function over plain data so the components stay
 * dumb and the record's wording can be asserted directly:
 *
 *  - AUDIO-10  the seek slider's keyboard geometry (arrows 5 s, Page 30 s,
 *              Home/End) and its `aria-valuetext`
 *  - AUDIO-21  the presence sentence, names only for enrolled participants and
 *              guests counted but never named
 *  - STATE-7   the one polite message a surface announces, derived ONLY from
 *              discrete state — never from a playback position, a seek step or
 *              a volume, so nothing announces per tick
 *  - AUDIO-3   ambience loops, a one-shot never does
 *
 * Types are derived from the wire contract's exported unions rather than
 * redeclared, so a contract change breaks here first.
 */

import type { CueKind, GmEvent } from './contracts'

type GmAudioEvent = Extract<GmEvent, { event: 'audio' }>
type PresenceEvent = Extract<GmEvent, { event: 'presence' }>

/** AUDIO-1: the table has exactly two slots. */
export type AudioSlot = GmAudioEvent['slot']
/** What a slot holds as the GM sees it — by id and title (AUDIO-11). */
export type GmPlaying = NonNullable<GmAudioEvent['playing']>
/** AUD-11: an alias travels only on the GM's authenticated channel. */
export type ParticipantPresence = PresenceEvent['participants'][number]
/** AUDIO-21: guests are counted, never named. */
export type GuestPresence = PresenceEvent['guests']
export type PresenceAudioState = ParticipantPresence['audio']

/** What the GM's own client knows about its stream (AUDIO-17). */
export type AudioConnection = 'connected' | 'reconnecting'
/** Whether the cue's bytes are usable yet (ADR MS-3, §12.2). */
export type CuePlayability = 'processing' | 'ready' | 'failed'

/** AUDIO-10: arrow keys move 5 s, Page keys 30 s. */
export const SEEK_ARROW_MS = 5_000
export const SEEK_PAGE_MS = 30_000

// ── Time ─────────────────────────────────────────────────────────────────────

/** `0:42`, `3:10`, `1:02:03`. Anything unusable reads as `0:00`. */
export function formatClock(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '0:00'
  const total = Math.floor(ms / 1000)
  const seconds = total % 60
  const minutes = Math.floor(total / 60) % 60
  const hours = Math.floor(total / 3600)
  const pad = (value: number) => String(value).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`
}

/** What is left, written the way a transport shows it: `−2:28`. */
export function formatRemaining(positionMs: number, durationMs: number): string {
  const minus = String.fromCodePoint(0x2212)
  return `${minus}${formatClock(Math.max(0, durationMs - positionMs))}`
}

/** AUDIO-10: the slider says where it is in words — `0:42 of 3:10`. */
export function seekValueText(positionMs: number, durationMs: number): string {
  return `${formatClock(positionMs)} of ${formatClock(durationMs)}`
}

/** The GM's own volume as a percentage, for the volume slider's value text. */
export function volumeValueText(volume: number): string {
  return `${Math.round(clampUnit(volume) * 100)}%`
}

// ── Seek geometry (AUDIO-10) ─────────────────────────────────────────────────

export function clampPositionMs(positionMs: number, durationMs: number): number {
  if (!Number.isFinite(positionMs)) return 0
  const ceiling = Number.isFinite(durationMs) && durationMs > 0 ? durationMs : 0
  return Math.min(ceiling, Math.max(0, positionMs))
}

/**
 * The position a seek key moves to, or `null` when the key is not one of ours —
 * so a component only swallows the keys it actually handles and Tab, Enter and
 * the rest keep working.
 */
export function nextSeekPositionMs(key: string, positionMs: number, durationMs: number): number | null {
  const at = clampPositionMs(positionMs, durationMs)
  switch (key) {
    case 'ArrowLeft':
    case 'ArrowDown':
      return clampPositionMs(at - SEEK_ARROW_MS, durationMs)
    case 'ArrowRight':
    case 'ArrowUp':
      return clampPositionMs(at + SEEK_ARROW_MS, durationMs)
    case 'PageDown':
      return clampPositionMs(at - SEEK_PAGE_MS, durationMs)
    case 'PageUp':
      return clampPositionMs(at + SEEK_PAGE_MS, durationMs)
    case 'Home':
      return 0
    case 'End':
      return clampPositionMs(durationMs, durationMs)
    default:
      return null
  }
}

/** Deterministic placeholder peaks, so a cue's silhouette never reflows. */
export function fallbackPeaks(count: number, seed: number): number[] {
  const bars = Math.max(0, Math.floor(count))
  const offset = Number.isFinite(seed) ? seed % 97 : 0
  return Array.from({ length: bars }, (_unused, index) => {
    const value = Math.abs(Math.sin((index + offset) * 1.7) * Math.cos((index + offset) * 0.6))
    return 0.18 + value * 0.82
  })
}

export function clampUnit(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

// ── Kinds and loops (AUDIO-3, AUDIO-4) ───────────────────────────────────────

/** AUDIO-3: a one-shot never loops, so it never gets a loop control at all. */
export function isLoopable(kind: CueKind): boolean {
  return kind === 'ambience'
}

/** The immutable kind, spelled for people (AUDIO-4). */
export function kindLabel(kind: CueKind): string {
  return kind === 'ambience' ? 'Ambience' : 'One-shot'
}

/** `Ambience · loops`, `Ambience · plays once`, `One-shot`. */
export function kindSummary(kind: CueKind, loop: boolean): string {
  if (!isLoopable(kind)) return kindLabel(kind)
  return `${kindLabel(kind)} · ${loop ? 'loops' : 'plays once'}`
}

// ── Presence (AUDIO-21, AUDIO-22) ────────────────────────────────────────────

export interface PresenceSummary {
  participants: readonly ParticipantPresence[]
  guests: GuestPresence
}

function joinNames(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? ''
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

/**
 * AUDIO-21 verbatim: `<n> listening · <n> muted · <alias> hasn't tapped in yet`.
 * Enrolled participants are named; guests are only ever counted. An `absent`
 * client is in none of the three counts — it has dropped (AUDIO-22).
 */
export function presenceSentence({ participants, guests }: PresenceSummary): string {
  const named = (state: PresenceAudioState) => participants.filter((one) => one.audio === state)
  const listening = named('listening').length + guests.listening
  const muted = named('muted').length + guests.muted
  const pendingAliases = named('pending').map((one) => one.alias)
  const pendingGuests = Math.max(0, guests.pending)

  const segments = [`${listening} listening`, `${muted} muted`]

  const waiting = [...pendingAliases]
  if (pendingGuests > 0) waiting.push(`${pendingGuests} guest${pendingGuests === 1 ? '' : 's'}`)
  if (waiting.length > 0) {
    const total = pendingAliases.length + pendingGuests
    segments.push(`${joinNames(waiting)} ${total === 1 ? "hasn't" : "haven't"} tapped in yet`)
  }

  return segments.join(' · ')
}

// ── The one polite message per surface (STATE-7) ──────────────────────────────

/**
 * The cue card's status line. Note what is NOT an input: the playback position,
 * the seek value and the volume. A tick or a seek step can therefore never
 * change this string, which is what keeps the live region quiet (STATE-7).
 */
export interface CueStatusInput {
  playability: CuePlayability
  live: boolean
  connection: AudioConnection
  pushFailed: boolean
  previewBlocked: boolean
}

export function cueStatusMessage({
  playability,
  live,
  connection,
  pushFailed,
  previewBlocked,
}: CueStatusInput): string | null {
  if (playability === 'failed') return "This file couldn't be processed"
  if (playability === 'processing') return 'Still processing…'
  if (pushFailed) return "Couldn't play to the table"
  if (previewBlocked) return 'Press play again to allow sound'
  if (connection === 'reconnecting') return 'Reconnecting…'
  if (live) return 'Playing to the table'
  return null
}

/** AUDIO-17: while the GM is disconnected the strip says so. Counts never here. */
export function stripStatusMessage(connection: AudioConnection): string | null {
  return connection === 'reconnecting' ? 'Reconnecting…' : null
}

/** §12.2, audio (player): what a table client is told while it waits or fails. */
export type ConsentStatus = 'idle' | 'loading' | 'error'

export function consentStatusMessage(status: ConsentStatus): string | null {
  if (status === 'loading') return 'Loading sound…'
  if (status === 'error') return "Your browser can't play this sound"
  return null
}

// ── Control enablement (AUDIO-8, AUDIO-17) ───────────────────────────────────

/**
 * AUDIO-17: Play to table is disabled while the GM is disconnected, and §12.2
 * disables it until the bytes are playable.
 */
export function canPlayToTable(playability: CuePlayability, connection: AudioConnection): boolean {
  return playability === 'ready' && connection === 'connected'
}

/**
 * AUDIO-17 and X-3: Stop is never disabled while disconnected, because then
 * liveness is unknown and silence must stay one action. While the stream is
 * healthy a cue that does not hold a slot has nothing to stop.
 */
export function canStopCue(live: boolean, connection: AudioConnection): boolean {
  return live || connection === 'reconnecting'
}
