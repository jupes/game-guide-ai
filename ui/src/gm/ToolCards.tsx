import type { ReactNode } from 'react'
import { SourceList } from '../components/SourceList'
import { GameContentCard, GameContentEntry, GameContentSection } from '../ds/GameContentCard'
import { StatBlockCard } from '../ds/StatBlockCard'
import { toHooksCardProps, toLootCardProps, toNamesCardProps, toRulesCardProps, toStatBlockCardProps } from './adapters'
import type { CardContent } from './contracts'
import { LANE_COPY } from './laneState'
import { toolById } from './registry'
import './ToolCards.css'

type Kind = Exclude<CardContent['card_kind'], 'stat_block'>

function Frame({ kind, title, children }: { kind: Kind; title: string; children: ReactNode }) {
  const tool = toolById(kind)
  return (
    <div className="tool-card" role="group" aria-label={tool?.label}>
      <GameContentCard kind={kind} title={title} kindLabel={tool?.label} icon={tool?.icon}
        accent="var(--md-sys-color-primary)" density="compact">
        <GameContentSection first density="compact">{children}</GameContentSection>
      </GameContentCard>
    </div>
  )
}

function LootCard({ title, items }: ReturnType<typeof toLootCardProps>) {
  return <Frame kind="loot" title={title}>
    <ul className="tool-card__list">
      {items.map(({ name, quantity, value, note }, index) => (
        <li key={index}>
          <strong>{name}</strong>
          {quantity !== undefined && <> ×{quantity}</>}
          {value !== undefined && <> · {value}</>}
          {note !== undefined && <div className="tool-card__prose">{note}</div>}
        </li>
      ))}
    </ul>
  </Frame>
}

function NamesCard({ title, entries }: ReturnType<typeof toNamesCardProps>) {
  return <Frame kind="names" title={title}>
    <ul className="tool-card__list">
      {entries.map(({ name, note }, index) => (
        <li key={index}><strong>{name}</strong>{note !== undefined && <> · {note}</>}</li>
      ))}
    </ul>
  </Frame>
}

function HooksCard({ title, entries }: ReturnType<typeof toHooksCardProps>) {
  return <Frame kind="hooks" title={title}>
    <ol className="tool-card__list">
      {entries.map(({ name, text }, index) => (
        <li key={index}><GameContentEntry name={name}><span className="tool-card__prose">{text}</span></GameContentEntry></li>
      ))}
    </ol>
  </Frame>
}

function RulesCard({ title, answer, sources, numbers }: ReturnType<typeof toRulesCardProps>) {
  return <Frame kind="rules" title={title}>
    <p className="tool-card__prose">{answer}</p>
    <SourceList sources={sources} numbers={numbers} />
  </Frame>
}

export function ToolCardBody({ card }: { card: CardContent }) {
  switch (card.card_kind) {
    case 'stat_block':
      return <div className="tool-card"><StatBlockCard density="compact" {...toStatBlockCardProps(card.stat_block)} /></div>
    case 'loot':
      return <LootCard {...toLootCardProps(card.loot)} />
    case 'names':
      return <NamesCard {...toNamesCardProps(card.names)} />
    case 'hooks':
      return <HooksCard {...toHooksCardProps(card.hooks)} />
    case 'rules':
      return <RulesCard {...toRulesCardProps(card.rules)} />
    default:
      void (card satisfies never)
      return <p className="assistant-lane__placeholder">{LANE_COPY.unsupportedResult}</p>
  }
}
