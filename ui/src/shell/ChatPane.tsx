/**
 * ChatPane — Mode-aware chat interface.
 *
 * Integrates useChat with the AppNav context to provide a fully-connected
 * conversation UI. Renders exchange history, a composer, and an export button.
 */

import * as React from 'react'
import { ChatMessage } from '../ds/ChatMessage'
import { TextField } from '../ds/TextField'
import { IconButton } from '../ds/IconButton'
import { Card } from '../ds/Card'
import { DiceRoll } from '../ds/DiceRoll'
import { SourceList } from '../components/SourceList'
import { useChat } from '../useChat'
import { exportChat } from '../exportChat'
import { useAppNav } from './AppNav'
import type { ChatMode } from '../api'
import type { PostFn } from '../useChat'

// ── Empty-state labels by mode ────────────────────────────────────────────────

const EMPTY_LABELS: Record<ChatMode, string> = {
  sage: 'Ask the Sage…',
  spell: 'Ask the Spell Archivist…',
  rules: 'Ask the Rules Arbiter…',
  gm: 'Ask the Game Master…',
}

// ── Dice notation parser ──────────────────────────────────────────────────────

interface DiceMatch {
  die: number
  value: number
  modifier: number
  total: number
}

function parseDiceNotation(text: string): DiceMatch | null {
  try {
    // Match patterns like "1d20+5=18" or "2d6 - 3 = 9"
    const match = /(\d+)d(\d+)(?:\s*[+−-]\s*(\d+))?\s*=\s*(\d+)/i.exec(text)
    if (!match) return null
    const die = parseInt(match[2], 10)
    const value = parseInt(match[1], 10) // rolled value (first number before d)
    const modifier = match[3] ? parseInt(match[3], 10) : 0
    const total = parseInt(match[4], 10)
    return { die, value, modifier, total }
  } catch {
    return null
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ChatPane({ post }: { post?: PostFn }): React.JSX.Element {
  const { mode, conversationId } = useAppNav()
  const { exchanges, send, pending } = useChat({ post, mode, conversationId })
  const [draft, setDraft] = React.useState('')

  const handleSend = React.useCallback(() => {
    const trimmed = draft.trim()
    if (!trimmed || pending) return
    send(trimmed)
    setDraft('')
  }, [draft, pending, send])

  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSend()
      }
    },
    [handleSend],
  )

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        gap: 8,
      }}
    >
      {/* Exchange list */}
      <div
        style={{
          flex: 1,
          overflow: 'hidden auto',
          display: 'flex',
          flexDirection: 'column',
          gap: 16,
          padding: '8px 0',
        }}
      >
        {exchanges.length === 0 ? (
          <p
            style={{
              margin: 'auto',
              fontSize: 18,
              color: 'var(--aether-on-surface-variant)',
              textAlign: 'center',
            }}
          >
            {EMPTY_LABELS[mode]}
          </p>
        ) : (
          exchanges.map((exchange) => (
            <React.Fragment key={exchange.id}>
              {/* Player prompt */}
              <ChatMessage role="player">{exchange.prompt}</ChatMessage>

              {/* DM response */}
              {exchange.status === 'pending' && (
                <ChatMessage role="dm">
                  <span role="status">Consulting the tomes…</span>
                </ChatMessage>
              )}

              {exchange.status === 'done' && exchange.response && (
                <>
                  <ChatMessage role="dm">{exchange.response.answer}</ChatMessage>

                  {/* Dice roll — parse answer for dice notation */}
                  {(() => {
                    const dice = parseDiceNotation(exchange.response.answer)
                    if (!dice || !exchange.response.answerable) return null
                    return (
                      <DiceRoll
                        die={dice.die}
                        value={dice.value}
                        modifier={dice.modifier}
                        style={{ alignSelf: 'flex-start', marginLeft: 56 }}
                      />
                    )
                  })()}

                  {/* Sources */}
                  {exchange.response.answerable && exchange.response.sources.length > 0 && (
                    <Card variant="outlined" padded={false} style={{ marginLeft: 56 }}>
                      <SourceList sources={exchange.response.sources} />
                    </Card>
                  )}
                </>
              )}

              {exchange.status === 'error' && (
                <ChatMessage role="system">{exchange.error}</ChatMessage>
              )}
            </React.Fragment>
          ))
        )}
      </div>

      {/* Toolbar: export button */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', flexShrink: 0 }}>
        <IconButton
          icon="download"
          ariaLabel="Export chat"
          onClick={() => exportChat(exchanges)}
        />
      </div>

      {/* Composer */}
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          gap: 8,
          flexShrink: 0,
        }}
      >
        <TextField
          multiline
          rows={2}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask…"
          disabled={pending}
          fullWidth
        />
        <IconButton
          icon="send"
          ariaLabel="Send message"
          onClick={handleSend}
          disabled={pending || draft.trim() === ''}
        />
      </div>
    </div>
  )
}
