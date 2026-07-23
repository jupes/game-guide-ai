/**
 * modes — Single source of truth for chat-mode metadata.
 *
 * Consumed by Landing (entry chips), LeftNav (mode filter chips), and
 * ChatPane (per-mode empty-state prompt).
 */

import type { ChatMode } from './AppNav'
import type { UserRole } from './currentUser'

/** Aetheril palette family used as a channel's accent (swe1.3). Mirrors the
 * Avatar `tone` vocabulary. DS-grounded: arcane is magic/spell-only, ember is
 * the DM/GM convention. */
export type ModeAccent = 'verdigris' | 'arcane' | 'gold' | 'ember'

export interface ModeEntry {
  mode: ChatMode
  /** Material Symbols Rounded ligature name. */
  icon: string
  label: string
  /** Empty-state prompt shown in ChatPane when a conversation has no exchanges. */
  emptyLabel: string
  /** Distinct channel accent, resolved to palette tokens in modeAccents.css. */
  accent: ModeAccent
}

export const MODES: readonly ModeEntry[] = [
  { mode: 'sage', icon: 'auto_stories', label: 'Sage', emptyLabel: 'Ask the Sage…', accent: 'verdigris' },
  { mode: 'spell', icon: 'auto_awesome', label: 'Spell', emptyLabel: 'Ask the Spell Archivist…', accent: 'arcane' },
  { mode: 'rules', icon: 'gavel', label: 'Rules', emptyLabel: 'Ask the Rules Arbiter…', accent: 'gold' },
  { mode: 'gm', icon: 'castle', label: 'GM', emptyLabel: 'Ask the Game Master…', accent: 'ember' },
]

/** Per-mode accent, derived from MODES. */
const ACCENT_BY_MODE: Record<ChatMode, ModeAccent> = Object.fromEntries(
  MODES.map((m) => [m.mode, m.accent]),
) as Record<ChatMode, ModeAccent>

/** CSS modifier class carrying a mode's accent tokens (see modeAccents.css). */
export function accentClass(mode: ChatMode): string {
  return `mode-accent--${ACCENT_BY_MODE[mode]}`
}

/** The modes a user of the given role may see: the GM channel is DM-only
 * (channel-chats CP-D — UI gating; server enforcement waits for real auth). */
export function modesForRole(role: UserRole): readonly ModeEntry[] {
  return role === 'dm' ? MODES : MODES.filter((m) => m.mode !== 'gm')
}

/** Per-mode empty-state labels, derived from MODES. */
export const EMPTY_LABELS: Record<ChatMode, string> = Object.fromEntries(
  MODES.map((m) => [m.mode, m.emptyLabel]),
) as Record<ChatMode, string> // fromEntries widens keys to string; MODES covers every ChatMode
