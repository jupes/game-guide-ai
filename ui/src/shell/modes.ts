/**
 * modes — Single source of truth for chat-mode metadata.
 *
 * Consumed by Landing (entry chips), LeftNav (mode filter chips), and
 * ChatPane (per-mode empty-state prompt).
 */

import type { ChatMode } from './AppNav'

export interface ModeEntry {
  mode: ChatMode
  /** Material Symbols Rounded ligature name. */
  icon: string
  label: string
  /** Empty-state prompt shown in ChatPane when a conversation has no exchanges. */
  emptyLabel: string
}

export const MODES: readonly ModeEntry[] = [
  { mode: 'sage', icon: 'auto_stories', label: 'Sage', emptyLabel: 'Ask the Sage…' },
  { mode: 'spell', icon: 'auto_awesome', label: 'Spell', emptyLabel: 'Ask the Spell Archivist…' },
  { mode: 'rules', icon: 'gavel', label: 'Rules', emptyLabel: 'Ask the Rules Arbiter…' },
  { mode: 'gm', icon: 'castle', label: 'GM', emptyLabel: 'Ask the Game Master…' },
]

/** Per-mode empty-state labels, derived from MODES. */
export const EMPTY_LABELS: Record<ChatMode, string> = Object.fromEntries(
  MODES.map((m) => [m.mode, m.emptyLabel]),
) as Record<ChatMode, string> // fromEntries widens keys to string; MODES covers every ChatMode
