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
import { parseDiceNotation } from './diceNotation'
import { EMPTY_LABELS } from './modes'
import type { LoadHistoryFn, PostFn } from '../useChat'
import './ChatPane.css'

// ── Component ─────────────────────────────────────────────────────────────────

export function ChatPane({
  post,
  loadHistory,
}: {
  post?: PostFn
  loadHistory?: LoadHistoryFn
}): React.JSX.Element {
  const { mode, conversationId } = useAppNav()
  const { exchanges, send, pending, historyError, loadingHistory } = useChat({
    post,
    loadHistory,
    mode,
    conversationId,
  })
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
    <div className="chat-pane">
      {/* Exchange list */}
      <div className="chat-pane__exchanges">
        {/* History recall failed — recoverable: the thread starts empty. */}
        {historyError && <ChatMessage role="system">{historyError}</ChatMessage>}

        {exchanges.length === 0 && loadingHistory ? (
          <p className="chat-pane__empty" role="status">
            Recalling the conversation…
          </p>
        ) : exchanges.length === 0 ? (
          !historyError && <p className="chat-pane__empty">{EMPTY_LABELS[mode]}</p>
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

                  {/* GM creative notice — answer is invented/extrapolated, not grounded */}
                  {mode === 'gm' && !exchange.response.answerable && (
                    <ChatMessage role="system">
                      ✦ Creative — may include invented content not drawn from the sources.
                    </ChatMessage>
                  )}

                  {/* Dice roll — parse answer for dice notation */}
                  {(() => {
                    const dice = parseDiceNotation(exchange.response.answer)
                    if (!dice || !exchange.response.answerable) return null
                    return (
                      <div className="chat-pane__dice">
                        <DiceRoll
                          die={dice.die}
                          value={dice.value}
                          modifier={dice.modifier}
                        />
                      </div>
                    )
                  })()}

                  {/* Sources */}
                  {exchange.response.answerable && exchange.response.sources.length > 0 && (
                    <Card variant="outlined" padded={false} className="chat-pane__sources">
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
      <div className="chat-pane__toolbar">
        <IconButton
          icon="download"
          ariaLabel="Export chat"
          onClick={() => exportChat(exchanges)}
        />
      </div>

      {/* Composer */}
      <div className="chat-pane__composer">
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
