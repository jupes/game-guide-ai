/**
 * GameContentCard(+Section+Entry) — the shared shell for canonical game
 * content quoted into chat (spells, stat blocks, and future types: items,
 * conditions). Header band (kicker + Cinzel title + qualifier + badge slot),
 * an optional stat strip of label/value cells, body sections, and an
 * optional citation footer. SpellCard/StatBlockCard compose this; they never
 * re-invent the chrome.
 */

import React from 'react'
import './GameContentCard.css'

// ── Props (mirrors the DS d.ts exactly) ──────────────────────────────────────

export interface GameContentCardProps {
  /** Drives the default kicker label, icon and accent. @default 'spell' */
  kind?: 'spell' | 'statblock' | (string & {})
  /** Override the ALL-CAPS kicker label. */
  kindLabel?: string
  /** Material Symbols ligature; defaults per kind. */
  icon?: string
  /** Accent color for the kicker icon; defaults per kind. */
  accent?: string
  /** Display name, set in Cinzel. */
  title: string
  /** Italic serif line under/beside the title ("3rd-level evocation"). */
  qualifier?: string
  /** Top-right slot — a Badge (CR, concentration, ritual…). */
  badge?: React.ReactNode
  /** Label/value cells for the stat strip. Omit for no strip. */
  stats?: Array<{ label: string; value: React.ReactNode }>
  /** Min column width of a stat cell in px. */
  minStatWidth?: number
  /** 'compact' for in-feed cards. @default 'default' */
  density?: 'default' | 'compact'
  /** Footer citation line; footer is omitted when absent. */
  source?: string
  /** Body sections — usually GameContentSection children. */
  children?: React.ReactNode
  style?: React.CSSProperties
}

export interface GameContentSectionProps {
  title?: string
  tone?: 'secondary' | 'muted'
  /** First section in the body — drops the top rule. */
  first?: boolean
  density?: 'default' | 'compact'
  children?: React.ReactNode
  style?: React.CSSProperties
}

export interface GameContentEntryProps {
  /** Entry name, rendered italic with a trailing period. */
  name?: string
  children?: React.ReactNode
  style?: React.CSSProperties
}

// ── Kind defaults ─────────────────────────────────────────────────────────────

const KINDS: Record<string, { label: string; icon: string; accent: string }> = {
  spell: { label: 'Spell', icon: 'auto_awesome', accent: 'var(--aether-arcane)' },
  statblock: { label: 'Stat Block', icon: 'shield', accent: 'var(--md-sys-color-primary)' },
}

// ── GameContentCard ───────────────────────────────────────────────────────────

export function GameContentCard({
  kind = 'spell',
  kindLabel,
  icon,
  accent,
  title,
  qualifier,
  badge,
  stats,
  minStatWidth,
  density = 'default',
  source,
  children,
  style,
}: GameContentCardProps): React.JSX.Element {
  const meta = KINDS[kind] ?? { label: '', icon: '', accent: '' }
  const compact = density === 'compact'
  const visibleStats = stats?.filter((s) => s.value !== null && s.value !== undefined && s.value !== '')

  return (
    <div className={`game-content-card game-content-card--${density}`} style={style}>
      <div className="game-content-card__header">
        {!compact && (
          <div className="game-content-card__kicker-row">
            <span
              className="material-symbols-rounded game-content-card__kicker-icon"
              style={{ color: accent ?? meta.accent }}
              aria-hidden="true"
            >
              {icon ?? meta.icon}
            </span>
            <span className="game-content-card__kicker-label">{kindLabel ?? meta.label}</span>
            {badge && <span className="game-content-card__badge-slot">{badge}</span>}
          </div>
        )}
        <div className="game-content-card__title-row">
          {compact && (
            <span
              className="material-symbols-rounded game-content-card__kicker-icon game-content-card__kicker-icon--compact"
              style={{ color: accent ?? meta.accent }}
              aria-hidden="true"
            >
              {icon ?? meta.icon}
            </span>
          )}
          <h3 className="game-content-card__title">{title}</h3>
          {qualifier && <span className="game-content-card__qualifier">{qualifier}</span>}
          {compact && badge && <span className="game-content-card__badge-slot">{badge}</span>}
        </div>
      </div>

      {visibleStats && visibleStats.length > 0 && (
        <div
          className="game-content-card__stats"
          style={{
            gridTemplateColumns: `repeat(auto-fit, minmax(${minStatWidth ?? (compact ? 104 : 138)}px, 1fr))`,
          }}
        >
          {visibleStats.map((s, i) => (
            <div key={i} className="game-content-card__stat-cell">
              <span className="game-content-card__stat-label">{s.label}</span>
              <span className="game-content-card__stat-value">{s.value}</span>
            </div>
          ))}
        </div>
      )}

      {children}

      {source && (
        <div className="game-content-card__footer">
          <span className="material-symbols-rounded game-content-card__footer-icon" aria-hidden="true">local_library</span>
          <span className="game-content-card__footer-text">{source}</span>
        </div>
      )}
    </div>
  )
}

// ── GameContentSection ────────────────────────────────────────────────────────

export function GameContentSection({
  title,
  tone = 'secondary',
  first = false,
  density = 'default',
  children,
  style,
}: GameContentSectionProps): React.JSX.Element {
  const classes = [
    'game-content-section',
    `game-content-section--${density}`,
    first ? 'game-content-section--first' : '',
  ].filter(Boolean).join(' ')
  return (
    <div className={classes} style={style}>
      {title && (
        <span className={`game-content-section__title game-content-section__title--${tone}`}>
          {title}
        </span>
      )}
      {children}
    </div>
  )
}

// ── GameContentEntry ──────────────────────────────────────────────────────────

export function GameContentEntry({ name, children, style }: GameContentEntryProps): React.JSX.Element {
  return (
    <p className="game-content-entry" style={style}>
      {name && <strong className="game-content-entry__name">{name}. </strong>}
      {children}
    </p>
  )
}
