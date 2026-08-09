/**
 * StatBlockCard — a 5e NPC/monster stat block on the GameContentCard shell.
 * The six-ability row is its signature moment: one cell per score, mono
 * numerals, modifier as a filled pill.
 */

import React from 'react'
import './StatBlockCard.css'
import { GameContentCard, GameContentSection, GameContentEntry } from './GameContentCard'
import { Badge } from './Badge'

// ── Props (mirrors the DS d.ts exactly) ──────────────────────────────────────

export interface StatBlockEntry {
  /** Entry name, rendered italic ("Multiattack"). */
  name: string
  text: string
}

export interface StatBlockCardProps {
  name: string
  /** "Medium", "Large", … */
  size?: string
  /** "undead", "fiend", … */
  type?: string
  alignment?: string
  ac?: number
  /** Parenthetical armor note ("barnacle plate"). */
  acNote?: string
  hp?: number
  /** "11d8 + 33" */
  hitDice?: string
  speed?: string
  /** Raw scores; modifiers are derived, never passed in. */
  abilities?: Partial<Record<'str' | 'dex' | 'con' | 'int' | 'wis' | 'cha', number>>
  savingThrows?: string
  skills?: string
  damageImmunities?: string
  conditionImmunities?: string
  senses?: string
  languages?: string
  cr?: string | number
  xp?: number
  traits?: StatBlockEntry[]
  actions?: StatBlockEntry[]
  bonusActions?: StatBlockEntry[]
  reactions?: StatBlockEntry[]
  legendaryActions?: StatBlockEntry[]
  density?: 'default' | 'compact'
  source?: string
  style?: React.CSSProperties
}

const ABILITIES = ['str', 'dex', 'con', 'int', 'wis', 'cha'] as const

const mod = (score: number): number => Math.floor((score - 10) / 2)
const fmt = (m: number): string => (m < 0 ? `−${Math.abs(m)}` : `+${m}`)

export function StatBlockCard({
  name,
  size,
  type,
  alignment,
  ac,
  acNote,
  hp,
  hitDice,
  speed,
  abilities = {},
  savingThrows,
  skills,
  damageImmunities,
  conditionImmunities,
  senses,
  languages,
  cr,
  xp,
  traits,
  actions,
  bonusActions,
  reactions,
  legendaryActions,
  density = 'default',
  source,
  style,
}: StatBlockCardProps): React.JSX.Element {
  const compact = density === 'compact'

  const details: Array<[string, string, boolean]> = (
    [
      ['Saving Throws', savingThrows, true],
      ['Skills', skills, true],
      ['Immunities', [damageImmunities, conditionImmunities].filter(Boolean).join('; '), false],
      ['Senses', senses, false],
      ['Languages', languages, false],
    ] as Array<[string, string | undefined, boolean]>
  ).filter((row): row is [string, string, boolean] => Boolean(row[1]))

  const sections: Array<[string, StatBlockEntry[]]> = (
    [
      ['Traits', traits], ['Actions', actions], ['Bonus Actions', bonusActions],
      ['Reactions', reactions], ['Legendary Actions', legendaryActions],
    ] as Array<[string, StatBlockEntry[] | undefined]>
  ).filter((row): row is [string, StatBlockEntry[]] => Boolean(row[1] && row[1].length))

  return (
    <GameContentCard
      kind="statblock"
      title={name}
      qualifier={compact ? `${size} ${type} · CR ${cr}` : `${size} ${type}, ${alignment}`}
      density={density}
      source={source}
      minStatWidth={150}
      badge={
        !compact && cr != null
          ? <Badge tone="gold">{`CR ${cr}${xp ? ` · ${xp.toLocaleString()} XP` : ''}`}</Badge>
          : undefined
      }
      stats={compact ? undefined : [
        {
          label: 'Armor Class',
          value: (
            <>
              {ac}
              {acNote && <span className="stat-block-card__ac-note"> ({acNote})</span>}
            </>
          ),
        },
        {
          label: 'Hit Points',
          value: (
            <span className="stat-block-card__hp">
              {hp}
              {hitDice && <span className="stat-block-card__hit-dice"> ({hitDice})</span>}
            </span>
          ),
        },
        { label: 'Speed', value: speed },
      ]}
      style={style}
    >
      {/* Signature: the six-ability row */}
      <div className={`stat-block-card__abilities stat-block-card__abilities--${density}`}>
        {ABILITIES.map((k) => {
          const score = abilities[k]
          const m = mod(score ?? 10)
          const positive = m > 0
          return (
            <div key={k} className={`stat-block-card__ability-cell stat-block-card__ability-cell--${density}`}>
              <span className="stat-block-card__ability-key">{k.toUpperCase()}</span>
              <span className="stat-block-card__ability-score">{score}</span>
              <span
                className={
                  'stat-block-card__ability-mod '
                  + (positive ? 'stat-block-card__ability-mod--positive' : 'stat-block-card__ability-mod--neutral')
                }
              >
                {fmt(m)}
              </span>
            </div>
          )
        })}
      </div>

      {compact ? (
        <div className="stat-block-card__compact-vitals">
          <span>{`AC ${ac}`}</span>
          <span className="stat-block-card__compact-hp">{`HP ${hp}`}</span>
          <span>{speed}</span>
        </div>
      ) : (
        <>
          {details.length > 0 && (
            <div className="stat-block-card__details">
              {details.map(([label, value, mono]) => (
                <div key={label} className="stat-block-card__detail-row">
                  <span className="stat-block-card__detail-label">{label}</span>
                  <span className={mono ? 'stat-block-card__detail-value--mono' : undefined}>{value}</span>
                </div>
              ))}
            </div>
          )}
          {sections.map(([title, entries], i) => (
            <GameContentSection key={title} title={title} first={i === 0}>
              {entries.map((e, j) => (
                <GameContentEntry key={j} name={e.name}>{e.text}</GameContentEntry>
              ))}
            </GameContentSection>
          ))}
          <div className="stat-block-card__spacer" />
        </>
      )}
    </GameContentCard>
  )
}
