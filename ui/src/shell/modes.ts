/**
 * modes — Single source of truth for chat-mode metadata.
 *
 * Consumed by Landing (entry chips), LeftNav (mode filter chips), and
 * ChatPane (per-mode empty-state prompt).
 */

import type { ChatMode } from './AppNav'
import type { UserRole } from './currentUser'

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

/** The modes a user of the given role may see: the GM channel is DM-only
 * (channel-chats CP-D — UI gating; server enforcement waits for real auth). */
export function modesForRole(role: UserRole): readonly ModeEntry[] {
  return role === 'dm' ? MODES : MODES.filter((m) => m.mode !== 'gm')
}

/** Per-mode empty-state labels, derived from MODES. */
export const EMPTY_LABELS: Record<ChatMode, string> = Object.fromEntries(
  MODES.map((m) => [m.mode, m.emptyLabel]),
) as Record<ChatMode, string> // fromEntries widens keys to string; MODES covers every ChatMode
