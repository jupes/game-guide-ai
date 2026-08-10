/**
 * SpellCard — a spell entry as written, rendered on the GameContentCard shell.
 */

import React from 'react'
import './SpellCard.css'
import { GameContentCard, GameContentSection } from './GameContentCard'
import { Chip } from './Chip'
import { Badge } from './Badge'

// ── Props (mirrors the DS d.ts exactly) ──────────────────────────────────────

export interface SpellCardProps {
  name: string
  /** 0 = cantrip. @default 0 */
  level?: number
  /** "evocation", "abjuration", … */
  school?: string
  castingTime?: string
  range?: string
  duration?: string
  /** V/S/M flags; `m` carries the material text. */
  components?: { v?: boolean; s?: boolean; m?: string }
  description?: string
  higherLevels?: string
  /** Class list, rendered as assist chips (full density only). */
  classes?: string[]
  concentration?: boolean
  ritual?: boolean
  density?: 'default' | 'compact'
  source?: string
  style?: React.CSSProperties
}

const ORDINAL = [
  'Cantrip', '1st-level', '2nd-level', '3rd-level', '4th-level',
  '5th-level', '6th-level', '7th-level', '8th-level', '9th-level',
]

export function SpellCard({
  name,
  level,
  school,
  castingTime,
  range,
  duration,
  components,
  description,
  higherLevels,
  classes,
  concentration = false,
  ritual = false,
  density = 'default',
  source,
  style,
}: SpellCardProps): React.JSX.Element {
  const compact = density === 'compact'
  // level is optional on the backend — defaulting an unknown level to 0
  // would misrepresent "we don't know" as "this is a cantrip" (PR #46
  // review). Only claim a level/cantrip qualifier when level is actually
  // present; fall back to school alone, or no qualifier at all.
  const qualifier = level == null
    ? (school || undefined)
    : (level === 0
        ? `${school ?? ''} cantrip`
        : `${ORDINAL[level] ?? `${level}th-level`} ${school ?? ''}`
      ).trim()

  const comp = [
    components?.v && 'V',
    components?.s && 'S',
    components?.m && 'M',
  ].filter(Boolean).join(' ')

  return (
    <GameContentCard
      kind="spell"
      title={name}
      qualifier={qualifier}
      density={density}
      source={source}
      badge={
        concentration
          ? <Badge tone="arcane">CONCENTRATION</Badge>
          : ritual
            ? <Badge tone="gold">RITUAL</Badge>
            : undefined
      }
      stats={[
        { label: compact ? 'Cast' : 'Casting Time', value: castingTime },
        { label: 'Range', value: range },
        { label: compact ? 'Comp.' : 'Components', value: comp },
        { label: 'Duration', value: duration },
      ]}
      style={style}
    >
      <GameContentSection first density={density}>
        {components?.m && !compact && (
          <div className="spell-card__material">
            <span className="spell-card__material-label">Material</span>
            <span className="spell-card__material-text">{components.m}</span>
          </div>
        )}
        <p className={`spell-card__description spell-card__description--${density}`}>
          {description}
        </p>
      </GameContentSection>

      {higherLevels && (
        <GameContentSection title="At Higher Levels" density={density}>
          <p className="spell-card__higher-levels">{higherLevels}</p>
        </GameContentSection>
      )}

      {classes && classes.length > 0 && !compact && (
        <GameContentSection density={density} style={{ paddingBottom: 20, borderTop: 'none', paddingTop: 14 }}>
          <div className="spell-card__classes">
            {classes.map((c) => <Chip key={c} label={c} type="assist" />)}
          </div>
        </GameContentSection>
      )}
      <div className={`spell-card__spacer spell-card__spacer--${density}`} />
    </GameContentCard>
  )
}
