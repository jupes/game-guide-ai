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

import type { InvocationStatus, ToolResult, ToolSuggestion } from './contracts'
import type { StatBlockCardProps } from '../ds/StatBlockCard'

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

/** Taken from the contract rather than re-declared, so it cannot drift from the wire. */
type StatBlockContent = Extract<ToolResult, { result_kind: 'card' }>['card']['stat_block']

/**
 * A `stat_block` card result as `StatBlockCard` wants it (1kg.3.2). The wire
 * says `ac_note` and `null`; the widget says `acNote` and `undefined`, and a
 * `null` reaching it would print as a literal gap (PR #46). Density is the
 * caller's: a lane renders `compact`, because the lane supplies the card chrome.
 */
export function toStatBlockCardProps(stat: StatBlockContent): StatBlockCardProps {
  return {
    name: stat.name,
    size: stat.size ?? undefined,
    type: stat.type ?? undefined,
    alignment: stat.alignment ?? undefined,
    ac: stat.ac,
    acNote: stat.ac_note ?? undefined,
    hp: stat.hp,
    hitDice: stat.hit_dice ?? undefined,
    speed: stat.speed ?? undefined,
    abilities: stat.abilities
      ? {
          str: stat.abilities.str ?? undefined,
          dex: stat.abilities.dex ?? undefined,
          con: stat.abilities.con ?? undefined,
          int: stat.abilities.int ?? undefined,
          wis: stat.abilities.wis ?? undefined,
          cha: stat.abilities.cha ?? undefined,
        }
      : undefined,
    savingThrows: stat.saving_throws ?? undefined,
    skills: stat.skills ?? undefined,
    damageImmunities: stat.damage_immunities ?? undefined,
    conditionImmunities: stat.condition_immunities ?? undefined,
    senses: stat.senses ?? undefined,
    languages: stat.languages ?? undefined,
    cr: stat.cr ?? undefined,
    xp: stat.xp ?? undefined,
    traits: stat.traits ?? undefined,
    actions: stat.actions ?? undefined,
    bonusActions: stat.bonus_actions ?? undefined,
    reactions: stat.reactions ?? undefined,
    legendaryActions: stat.legendary_actions ?? undefined,
  }
}
