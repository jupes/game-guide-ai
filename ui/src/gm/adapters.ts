/**
 * Wire → design-system adapters for the Workbench (1kg.1.2).
 *
 * The API speaks snake_case and its own vocabulary; the design-system widgets
 * keep the camelCase props of their handoff `.d.ts`. This module is where the
 * two meet for Workbench payloads, the way `toSpellCardProps` and
 * `toStatBlockCardProps` are for `/chat` — so neither convention leaks into the
 * other, and a rename on either side is one edit.
 *
 * It holds only what the contract slice that exists needs. Each later slice
 * (timeline, documents, reveal, audio) adds its adapters here beside its schemas.
 */

import type { InvocationStatus, ToolSuggestion } from './contracts'

/** `AssistantLaneProps.status`, plus the `cancelled` state decision RAIL-22 adds. */
export type LaneStatus = 'working' | 'done' | 'error' | 'cancelled'

/** `LaneSuggestion` from the handoff's `AssistantLane.d.ts`, with `id` required. */
export interface LaneSuggestion {
  id: string
  label: string
  icon?: string
}

/** The wire says `failed` — a resource state; the lane says `error` — a look. */
export function toLaneStatus(status: InvocationStatus): LaneStatus {
  return status === 'failed' ? 'error' : status
}

/**
 * Chips for a lane. `id` is the suggestion's index, because two suggestions may
 * target the same tool with different briefs; the controller that arms the
 * composer (RAIL-8) looks the wire suggestion up by it with `suggestionFor`.
 */
export function toLaneSuggestions(suggestions: readonly ToolSuggestion[]): LaneSuggestion[] {
  return suggestions.map((suggestion, index) => ({
    id: String(index),
    label: suggestion.label,
    ...(suggestion.icon ? { icon: suggestion.icon } : {}),
  }))
}

/** The wire suggestion behind a chip, or `undefined` for an id this list never issued. */
export function suggestionFor(
  suggestions: readonly ToolSuggestion[],
  chip: Pick<LaneSuggestion, 'id'>,
): ToolSuggestion | undefined {
  return /^\d+$/.test(chip.id) ? suggestions[Number(chip.id)] : undefined
}
