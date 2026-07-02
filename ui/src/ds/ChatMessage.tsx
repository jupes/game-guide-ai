/**
 * ChatMessage — Aetheril campaign chat turn.
 *
 * Three roles:
 *   'dm'     — AI Dungeon Master; Spectral serif bubble; left-aligned; gilt avatar ring
 *   'player' — Human player; ember-primary bubble; right-aligned
 *   'system' — Centered muted pill notice (dice results, quest events, etc.)
 *
 * Asymmetric bubble radius (the "tail corner" approach):
 *   The corner of the bubble closest to the avatar is squared to visually
 *   anchor the speech bubble to the speaker.
 *     DM     (avatar left)  → top-left  corner  = extra-small radius
 *     Player (avatar right) → top-right corner  = extra-small radius
 *   All other corners use the large radius for a soft, friendly appearance.
 */

import React from 'react'
import { deriveInitials } from './Avatar'
import './ChatMessage.css'

// ── Props (mirrors the DS d.ts exactly) ──────────────────────────────────────

export interface ChatMessageProps {
  role?: 'dm' | 'player' | 'system'
  /** Display name; defaults to "Dungeon Master" / "You" by role. */
  author?: string
  /** Avatar image URL. */
  avatar?: string
  /** Material Symbols Rounded ligature name for the avatar fallback. */
  avatarIcon?: string
  /** Timestamp label, e.g. "8:42 PM". */
  time?: string
  children?: React.ReactNode
  style?: React.CSSProperties
}

// ── Avatar sub-component (inline — no DS bundle dep) ─────────────────────────

interface AvatarProps {
  role: 'dm' | 'player'
  author?: string
  avatar?: string
  avatarIcon?: string
}

function MessageAvatar({ role, author, avatar, avatarIcon }: AvatarProps) {
  // Initials fallback — shared with the Avatar DS component
  const initials = deriveInitials(author ?? (role === 'dm' ? 'Dungeon Master' : 'You'))

  return (
    <div className="chat-message__avatar" aria-hidden="true">
      {avatar ? (
        <img src={avatar} alt={author ?? ''} />
      ) : avatarIcon ? (
        <span className="chat-message__avatar-icon material-symbols-rounded">
          {avatarIcon}
        </span>
      ) : role === 'dm' ? (
        // DM default: book icon ligature
        <span className="chat-message__avatar-icon material-symbols-rounded">
          auto_stories
        </span>
      ) : (
        <span>{initials}</span>
      )}
    </div>
  )
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ChatMessage({
  role = 'dm',
  author,
  avatar,
  avatarIcon,
  time,
  children,
  style,
}: ChatMessageProps): React.JSX.Element {
  // ── System notice ─────────────────────────────────────────────────────────
  if (role === 'system') {
    return (
      <div className="chat-message chat-message--system" style={style}>
        <div className="chat-message__system-pill">
          {children}
        </div>
      </div>
    )
  }

  // ── DM or Player ─────────────────────────────────────────────────────────
  const isPlayer      = role === 'player'
  const displayAuthor = author ?? (role === 'dm' ? 'Dungeon Master' : 'You')

  // Bubble variant classes
  const bubbleToneClass  = isPlayer ? 'chat-message__bubble--ember' : 'chat-message__bubble--body-serif'
  const bubbleTailClass  = isPlayer ? 'chat-message__bubble--tail-right' : 'chat-message__bubble--tail-left'

  return (
    <div
      className={`chat-message chat-message--${role}`}
      style={style}
    >
      <MessageAvatar
        role={role}
        author={displayAuthor}
        avatar={avatar}
        avatarIcon={avatarIcon}
      />

      <div className="chat-message__content">
        <div className="chat-message__meta">
          <span className="chat-message__author">{displayAuthor}</span>
          {time && <span className="chat-message__time">{time}</span>}
        </div>

        <div className={`chat-message__bubble ${bubbleToneClass} ${bubbleTailClass}`}>
          {children}
        </div>
      </div>
    </div>
  )
}
