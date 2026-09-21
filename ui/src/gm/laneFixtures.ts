/**
 * Wire payloads for the assistant lane's tests and stories (1kg.3.2).
 *
 * Every fixture goes through `ToolInvocationSchema.parse`, so a payload that
 * could not come off the wire fails here rather than propping up a green test.
 * That matters most for the states the lane must make impossible: the schema
 * already refuses a `working` invocation carrying a result, and these builders
 * inherit the refusal.
 *
 * Shared by the unit tests and the Storybook stories so a state cannot be
 * demonstrated with one payload and asserted with another.
 */

import { ToolInvocationSchema } from './contracts'
import type { ToolInvocation, ToolSuggestion } from './contracts'
import { toolAvailability } from './registry'
import type { LaneTimer } from './laneState'

const BASE = {
  schema_version: 1,
  invocation_id: 'inv_9f2c4e1a7b3d4c5e',
  tool_id: 'npc',
  status: 'working',
  attempt: 1,
  cancel_requested: false,
  created_at: '2026-09-16T19:31:02Z',
  updated_at: '2026-09-16T19:31:24Z',
  result: null,
  error: null,
}

/** A valid `ToolInvocation`; anything not overridden is a working `/npc` run. */
export function toolInvocation(overrides: Record<string, unknown> = {}): ToolInvocation {
  return ToolInvocationSchema.parse({ ...BASE, ...overrides })
}

export const DOCUMENT_LINK = {
  document_id: 'doc_4b1d9e7a',
  type: 'npc',
  title: 'Ondrey the Ferryman',
  library_category: 'npcs',
}

export function documentResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    result_kind: 'document',
    tool_id: 'npc',
    prose: 'A ferryman who remembers every debt. He will not say whose.',
    suggestions: [],
    document: DOCUMENT_LINK,
    ...overrides,
  }
}

export function cardResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    result_kind: 'card',
    tool_id: 'monster',
    prose: 'Built from the Tidewarden thread — **grapple pressure**, not raw damage.',
    suggestions: [],
    card: {
      card_kind: 'stat_block',
      stat_block: {
        name: 'Drowned Thing',
        ac: 14,
        hp: 76,
        size: 'Large',
        type: 'undead',
        alignment: 'neutral evil',
        hit_dice: '9d10 + 27',
        speed: '20 ft., swim 40 ft.',
        abilities: { str: 18, dex: 10, con: 16, int: 6, wis: 10, cha: 5 },
        cr: 5,
        xp: 1800,
        actions: [{ name: 'Drag Under', text: 'One grappled creature is pulled 15 feet into the water.' }],
      },
    },
    ...overrides,
  }
}

export function mediaResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    result_kind: 'media',
    tool_id: 'portrait',
    prose: 'A study in lamplight.',
    suggestions: [],
    asset: { asset_id: 'ast_7c2b', media_type: 'image', alt: 'A ferryman at his oar' },
    ...overrides,
  }
}

export const SUGGESTIONS: readonly ToolSuggestion[] = [
  { tool_id: 'encounter', label: 'Build an encounter around it', icon: 'swords', brief: 'an ambush at the crossing' },
  { tool_id: 'hooks', label: 'Three hooks', icon: 'flag', brief: null },
  { tool_id: 'monster', label: 'Stat his dog', icon: 'shield', brief: null },
]

export function errorInfo(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    code: 'backend_unavailable',
    message: "Aetheril can't reach its library right now. Nothing was lost.",
    retryable: true,
    ...overrides,
  }
}

/** Every tool this deployment could run. Suggestions are validated against it (RAIL-8). */
export const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })

/** The shipped default: the two image tools have no approved provider (record 3.3). */
export const DEFAULT_AVAILABILITY = toolAvailability({})

// ── A clock the tests drive ──────────────────────────────────────────────────

export interface ManualTimer extends LaneTimer {
  /** Runs every live callback, oldest first. */
  fire(): void
  /** The delays currently waiting, in schedule order. */
  readonly pending: readonly number[]
}

/**
 * RAIL-15's 30 s, without waiting 30 s. `fire()` is the whole point: a test
 * moves the lane past the threshold in one synchronous call, so the assertion
 * is about the label rather than about timing.
 */
export function manualTimer(): ManualTimer {
  const entries: { ms: number; fn: () => void; live: boolean }[] = []
  return {
    schedule(ms, fn) {
      const entry = { ms, fn, live: true }
      entries.push(entry)
      return () => {
        entry.live = false
      }
    },
    fire() {
      for (const entry of entries) {
        if (!entry.live) continue
        entry.live = false
        entry.fn()
      }
    },
    get pending() {
      return entries.filter((entry) => entry.live).map((entry) => entry.ms)
    },
  }
}
