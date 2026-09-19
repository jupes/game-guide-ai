/**
 * Rail pins (1kg.3.3) — RAIL-10, RAIL-11, RAIL-12.
 *
 * Pins are an ordered list of 0–5 unique, known tool ids, stored **per account,
 * per device** in `localStorage`, the same posture as display name and avatar
 * tone today. They hold **tool ids only** — never a draft, a brief or anything
 * else a GM typed (X-7). Server-synced preferences are epic `1ka`'s.
 *
 * Reading is `normalisePins` from the registry, so every rule (unknown ids
 * dropped, no back-filling, absent means the defaults, an empty list means the
 * GM deliberately emptied the rail) lives in one place. Writing adds the one
 * rule reading cannot have: a tool must be **enabled at the moment it is
 * pinned** (RAIL-10). A stored pin that later becomes disabled keeps its place
 * and still counts toward five, so re-enabling restores the rail unchanged.
 */

import * as React from 'react'
import type { ToolId } from './contracts'
import { RAIL_LIMIT, REGISTRY, normalisePins } from './registry'
import type { ToolAvailability } from './registry'

const STORAGE_PREFIX = 'game-guide-ai:gm-pins'

/** The signed-out bucket, matching `conversationStore`'s guest posture. */
export const GUEST_USER_ID = 'guest'

/** RAIL-12: per account, per device. */
export function pinsStorageKey(userId: string): string {
  return `${STORAGE_PREFIX}:${userId}`
}

/**
 * The stored pins for this account, or `DEFAULT_PINNED` when there are none.
 *
 * Every failure mode lands on the defaults rather than on an empty rail:
 * storage unavailable (private mode), a quota error on read, a truncated JSON
 * payload, or a value some other writer left behind.
 */
export function readPins(userId: string): ToolId[] {
  try {
    const raw = localStorage.getItem(pinsStorageKey(userId))
    // Absent means "use DEFAULT_PINNED" (RAIL-11); `[]` means the GM emptied
    // the rail on purpose, and `normalisePins` keeps those apart.
    return normalisePins(raw === null ? undefined : JSON.parse(raw))
  } catch {
    return normalisePins(undefined)
  }
}

/** Persist the pins. Returns false when the device refused to store them — the
 * rail still works for this session, which is why nothing here throws. */
export function writePins(userId: string, pins: readonly ToolId[]): boolean {
  try {
    localStorage.setItem(pinsStorageKey(userId), JSON.stringify([...pins]))
    return true
  } catch {
    // Quota exceeded, or storage unavailable (private mode). A lost preference
    // is not worth taking the composer down for.
    return false
  }
}

/** RAIL-10 + RAIL-11: at most five, unique, and enabled at the moment it is pinned. */
export function canPin(
  pins: readonly ToolId[],
  toolId: ToolId,
  availability: Readonly<Record<ToolId, ToolAvailability>>,
): boolean {
  if (pins.includes(toolId)) return false
  if (pins.length >= RAIL_LIMIT) return false
  return availability[toolId]?.enabled === true
}

/** SLASH-15's toggle. Unpinning always works — including for a tool that has
 * since been disabled; pinning obeys `canPin`. Returns the same array when the
 * toggle is refused, so a caller can tell nothing happened. */
export function togglePin(
  pins: readonly ToolId[],
  toolId: ToolId,
  availability: Readonly<Record<ToolId, ToolAvailability>>,
): ToolId[] {
  if (pins.includes(toolId)) return pins.filter((id) => id !== toolId)
  if (!canPin(pins, toolId, availability)) return [...pins]
  return [...pins, toolId]
}

/** SLASH-15: reordering is never drag-only. `-1` is up (earlier), `1` is down. */
export function movePin(pins: readonly ToolId[], toolId: ToolId, direction: -1 | 1): ToolId[] {
  const from = pins.indexOf(toolId)
  const to = from + direction
  if (from === -1 || to < 0 || to >= pins.length) return [...pins]
  const next = [...pins]
  next.splice(from, 1)
  next.splice(to, 0, toolId)
  return next
}

/** SLASH-15's Reset to defaults. */
export function defaultPins(): ToolId[] {
  return [...REGISTRY.default_pinned]
}

export interface PinStore {
  /** The rail's order, already normalised (RAIL-11). */
  pins: ToolId[]
  /** Replace the whole list — SLASH-15's Save. Normalised and persisted. */
  savePins: (next: readonly ToolId[]) => void
  /** False when the last save could not be persisted (quota, private mode). */
  persisted: boolean
}

/**
 * The rail's pins for one account. `userId` scopes the storage key, so signing
 * in as somebody else on the same browser never shows the previous GM's rail.
 *
 * Storage is read during render rather than in an effect, so the first paint
 * already has the right rail; a save updates the in-memory copy and the device
 * together.
 */
export function usePins(userId: string = GUEST_USER_ID): PinStore {
  const [saved, setSaved] = React.useState<{ userId: string; pins: ToolId[]; persisted: boolean } | null>(null)

  const pins = React.useMemo(
    () => (saved !== null && saved.userId === userId ? saved.pins : readPins(userId)),
    [saved, userId],
  )

  const savePins = React.useCallback(
    (next: readonly ToolId[]) => {
      const normalised = normalisePins([...next])
      setSaved({ userId, pins: normalised, persisted: writePins(userId, normalised) })
    },
    [userId],
  )

  return { pins, savePins, persisted: saved === null || saved.userId !== userId ? true : saved.persisted }
}
