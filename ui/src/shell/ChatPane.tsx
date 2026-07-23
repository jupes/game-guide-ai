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
import { Chip } from '../ds/Chip'
import { DiceRoll } from '../ds/DiceRoll'
import { SourceList } from '../components/SourceList'
import { useChat } from '../useChat'
import { exportChat } from '../exportChat'
import { useAppNav } from './AppNav'
import { parseDiceNotation } from './diceNotation'
import { EMPTY_LABELS } from './modes'
import {
  getAttachments as defaultGetAttachments,
  uploadAttachment as defaultUploadAttachment,
} from '../api'
import type { Attachment, AttachmentsResult, Suggestion, UploadAttachmentResult } from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'
import './ChatPane.css'

// ── File attachments (swe1.6) ────────────────────────────────────────────────

export type UploadAttachmentFn = (conversationId: string, file: File) => Promise<UploadAttachmentResult>
export type GetAttachmentsFn = (conversationId: string) => Promise<AttachmentsResult>

// Spell-usage suggestion cards (channel-chats CP-C) — LLM inventions rendered
// apart from the literal spell text so quoted rules stay visibly verbatim.
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

function SuggestionCards({ suggestions }: { suggestions: Suggestion[] }): React.JSX.Element {
  return (
    <Card variant="outlined" className="chat-pane__suggestions">
      <ul className="chat-pane__suggestion-list">
        {suggestions.map((s) => (
          <li key={s.style} className="chat-pane__suggestion">
            <Chip type="suggestion" label={SUGGESTION_LABELS[s.style]} icon={SUGGESTION_ICONS[s.style]} />
            <span>{s.text}</span>
          </li>
        ))}
      </ul>
    </Card>
  )
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ChatPane({
  post,
  loadHistory,
  uploadAttachment = defaultUploadAttachment,
  getAttachments = defaultGetAttachments,
}: {
  post?: PostFn
  loadHistory?: LoadHistoryFn
  uploadAttachment?: UploadAttachmentFn
  getAttachments?: GetAttachmentsFn
}): React.JSX.Element {
  const { mode, conversationId } = useAppNav()
  const { exchanges, send, pending, historyError, loadingHistory } = useChat({
    post,
    loadHistory,
    mode,
    conversationId,
  })
  const [draft, setDraft] = React.useState('')
  // Scoped like useChat's history state: derive "this scope's attachments" from
  // scopeId===conversationId rather than resetting via setState-in-effect (a
  // synchronous setState in an effect body triggers cascading renders).
  const [attachmentState, setAttachmentState] = React.useState<{
    scopeId: string | null
    attachments: Attachment[]
  }>({ scopeId: conversationId, attachments: [] })
  const attachments = attachmentState.scopeId === conversationId ? attachmentState.attachments : []
  const [attachmentError, setAttachmentError] = React.useState<string | null>(null)
  const fileInputRef = React.useRef<HTMLInputElement>(null)

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

  // Load a conversation's previously-attached files when it opens. Pure
  // derivation above already shows an empty row for a new/no scope; the
  // effect only does the async fetch, never a synchronous setState.
  React.useEffect(() => {
    if (conversationId === null) return
    let cancelled = false
    void getAttachments(conversationId).then(
      (result) => {
        if (cancelled) return
        if (result.kind === 'ok') {
          setAttachmentState({ scopeId: conversationId, attachments: result.attachments })
        }
      },
      // A rejecting GetAttachmentsFn degrades like an error result (chips just
      // don't show) — an unhandled rejection here would take the pane down.
      () => {},
    )
    return () => {
      cancelled = true
    }
  }, [conversationId, getAttachments])

  const handleFileSelected = React.useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0]
      e.target.value = '' // allow re-selecting the same file later
      if (!file || conversationId === null) return
      setAttachmentError(null)
      void uploadAttachment(conversationId, file).then(
        (result) => {
          if (result.kind === 'ok') {
            setAttachmentState((prev) => ({
              scopeId: conversationId,
              attachments: [
                ...(prev.scopeId === conversationId ? prev.attachments : []),
                result.attachment,
              ],
            }))
          } else {
            setAttachmentError(result.message)
          }
        },
        // A rejecting UploadAttachmentFn surfaces like an error result instead
        // of vanishing into an unhandled rejection (useChat's posture).
        () => setAttachmentError("Couldn't upload the file — please try again."),
      )
    },
    [conversationId, uploadAttachment],
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

                  {/* Spell-usage suggestions — rendered apart from the answer */}
                  {exchange.response.suggestions && exchange.response.suggestions.length > 0 && (
                    <SuggestionCards suggestions={exchange.response.suggestions} />
                  )}

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

      {/* Attachments — files attached to this conversation (swe1.6) */}
      {attachments.length > 0 && (
        <div className="chat-pane__attachments">
          {attachments.map((a) => (
            <Chip key={a.id} type="assist" icon="description" label={a.filename} />
          ))}
        </div>
      )}
      {attachmentError && <ChatMessage role="system">{attachmentError}</ChatMessage>}

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
        <input
          ref={fileInputRef}
          type="file"
          // Mirrors the service's ATTACHMENT_TYPES allowlist (server-side check
          // remains the source of truth; this only pre-filters the picker).
          accept=".txt,.md,.pdf"
          aria-label="Attach file"
          className="chat-pane__file-input"
          onChange={handleFileSelected}
        />
        <IconButton
          icon="attach_file"
          ariaLabel="Attach file"
          onClick={() => fileInputRef.current?.click()}
          disabled={conversationId === null}
        />
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
