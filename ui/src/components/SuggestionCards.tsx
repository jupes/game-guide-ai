import * as React from 'react'
import { Card } from '../ds/Card'
import { Chip } from '../ds/Chip'
import type { Suggestion } from '../api'
import './SuggestionCards.css'

// Spell-usage suggestion cards (channel-chats CP-C) — LLM inventions rendered
// apart from the literal spell text so quoted rules stay visibly verbatim.
// Shared by ChatPane's feed and the GM thread's lane (agent-forge-harness-0ru).
const SUGGESTION_LABELS: Record<Suggestion['style'], string> = {
  practical: 'Practical',
  roleplay: 'Roleplay',
  wacky: 'Wacky',
}

const SUGGESTION_ICONS: Record<Suggestion['style'], string> = {
  practical: 'target',
  roleplay: 'theater_comedy',
  wacky: 'celebration',
}

/** Renders nothing when there are no suggestions, like `SourceList`. */
export function SuggestionCards({
  suggestions,
  className,
}: {
  suggestions: readonly Suggestion[] | null | undefined
  /** The caller's placement in its own layout. */
  className?: string
}): React.JSX.Element | null {
  if (!suggestions || suggestions.length === 0) return null
  return (
    <Card variant="outlined" className={className ? `suggestion-cards ${className}` : 'suggestion-cards'}>
      <ul className="suggestion-cards__list">
        {suggestions.map((s) => (
          <li key={s.style} className="suggestion-cards__item">
            <Chip type="suggestion" label={SUGGESTION_LABELS[s.style]} icon={SUGGESTION_ICONS[s.style]} />
            <span>{s.text}</span>
          </li>
        ))}
      </ul>
    </Card>
  )
}
