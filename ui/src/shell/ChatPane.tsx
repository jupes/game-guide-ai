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
import { SpellCard } from '../ds/SpellCard'
import type { SpellCardProps } from '../ds/SpellCard'
import { StatBlockCard } from '../ds/StatBlockCard'
import type { StatBlockCardProps } from '../ds/StatBlockCard'
import { SourceList } from '../components/SourceList'
import { Markdown } from '../components/Markdown'
import { useChat } from '../useChat'
import { exportChat } from '../exportChat'
import { useAppNav } from './AppNav'
import { useConversationStore } from './ConversationStoreContext'
import { parseDiceNotation } from './diceNotation'
import { EMPTY_LABELS } from './modes'
import {
  getAttachments as defaultGetAttachments,
  uploadAttachment as defaultUploadAttachment,
} from '../api'
import type {
  Attachment,
  AttachmentsResult,
  SpellContent,
  StatBlockContent,
  Suggestion,
  UploadAttachmentResult,
} from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'
import './ChatPane.css'

// ── z7fl.4 — snake_case (wire) -> camelCase (DS widget prop) adapters ────────
// A local mapping, not a widget-contract or wire-format change: the ported
// widgets keep their DS camelCase props unchanged (mirrors the .d.ts
// exactly); the API stays snake_case like every other field. This is the
// only place the two conventions meet.

function toSpellCardProps(sc: SpellContent): SpellCardProps {
  return {
    name: sc.name,
    level: sc.level ?? undefined,
    school: sc.school ?? undefined,
    castingTime: sc.casting_time ?? undefined,
    range: sc.range ?? undefined,
    duration: sc.duration ?? undefined,
    components: sc.components
      ? {
          v: sc.components.v ?? undefined,
          s: sc.components.s ?? undefined,
          m: sc.components.m ?? undefined,
        }
      : undefined,
    description: sc.description,
    higherLevels: sc.higher_levels ?? undefined,
    classes: sc.classes ?? undefined,
    concentration: sc.concentration ?? undefined,
    ritual: sc.ritual ?? undefined,
  }
}

function toStatBlockCardProps(sb: StatBlockContent): StatBlockCardProps {
  return {
    name: sb.name,
    size: sb.size ?? undefined,
    type: sb.type ?? undefined,
    alignment: sb.alignment ?? undefined,
    ac: sb.ac,
    acNote: sb.ac_note ?? undefined,
    hp: sb.hp,
    hitDice: sb.hit_dice ?? undefined,
    speed: sb.speed ?? undefined,
    abilities: sb.abilities
      ? {
          str: sb.abilities.str ?? undefined,
          dex: sb.abilities.dex ?? undefined,
          con: sb.abilities.con ?? undefined,
          int: sb.abilities.int ?? undefined,
          wis: sb.abilities.wis ?? undefined,
          cha: sb.abilities.cha ?? undefined,
        }
      : undefined,
    savingThrows: sb.saving_throws ?? undefined,
    skills: sb.skills ?? undefined,
    damageImmunities: sb.damage_immunities ?? undefined,
    conditionImmunities: sb.condition_immunities ?? undefined,
    senses: sb.senses ?? undefined,
    languages: sb.languages ?? undefined,
    cr: sb.cr ?? undefined,
    xp: sb.xp ?? undefined,
    traits: sb.traits ?? undefined,
    actions: sb.actions ?? undefined,
    bonusActions: sb.bonus_actions ?? undefined,
    reactions: sb.reactions ?? undefined,
    legendaryActions: sb.legendary_actions ?? undefined,
  }
}

// ── Autoscroll (pp6q.1.3) ────────────────────────────────────────────────────
// Follow the newest message ONLY while the reader is already at the bottom.
// Scrolling on every render is the classic failure of this feature: it yanks
// the view out from under someone reading earlier history.
//
// 32px rather than an exact equality check — fractional scroll offsets are
// routine under browser zoom and HiDPI, so `scrollTop === scrollHeight -
// clientHeight` would classify a reader who never moved as "scrolled away"
// and silently stop following.
const AT_BOTTOM_THRESHOLD_PX = 32

function distanceFromBottom(el: HTMLElement): number {
  return el.scrollHeight - el.clientHeight - el.scrollTop
}

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
  const { mode, conversationId, setConversationId } = useAppNav()
  const conversationStore = useConversationStore()
  const { exchanges, send, pending, historyError, loadingHistory } = useChat({
    post,
    loadHistory,
    mode,
    conversationId,
    onConversationAdopted: setConversationId,
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

  // Autoscroll (pp6q.1.3). A fresh thread starts at the bottom by definition.
  const feedRef = React.useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = React.useState(true)

  const scrollToLatest = React.useCallback(() => {
    const feed = feedRef.current
    if (!feed) return
    feed.scrollTop = feed.scrollHeight
    setAtBottom(true)
  }, [])

  const handleFeedScroll = React.useCallback(() => {
    const feed = feedRef.current
    if (!feed) return
    setAtBottom(distanceFromBottom(feed) <= AT_BOTTOM_THRESHOLD_PX)
  }, [])

  // Follow new content only when the reader is already at the bottom. Keyed on
  // exchanges (new turn, or a pending turn resolving into a longer answer) and
  // on the scope, so opening a conversation lands at its newest message.
  React.useEffect(() => {
    if (!atBottom) return
    const feed = feedRef.current
    if (!feed) return
    feed.scrollTop = feed.scrollHeight
    // `atBottom` is intentionally NOT a dependency: this must run when the
    // content changes, not when the flag flips. Including it would re-scroll
    // the instant a reader scrolled back down, before new content arrived.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exchanges, conversationId])

  const handleSend = React.useCallback(() => {
    const trimmed = draft.trim()
    if (!trimmed || pending) return
    if (conversationId !== null) {
      conversationStore.recordFirstPrompt(conversationId, trimmed)
    }
    send(trimmed)
    setDraft('')
  }, [conversationId, conversationStore, draft, pending, send])

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
      {/* Exchange list. The scroller carries the parchment ground (the DS ships
          .aether-parchment and its own ChatView mock applies it to the feed);
          the inner __column is the centered reading measure, so prose does not
          run the full width of a wide viewport. */}
      <div
        className="chat-pane__exchanges aether-parchment"
        ref={feedRef}
        onScroll={handleFeedScroll}
      >
        <div className="chat-pane__column">
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
                  <ChatMessage role="dm">
                    {/* Model output — rendered through DOMPurify, never raw
                        (pp6q.1.1). See components/Markdown.tsx. */}
                    <Markdown source={exchange.response.answer} />
                  </ChatMessage>

                  {/* Structured content (z7fl.4) is additive, alongside the
                      prose — NOT a replacement (PR #46 review). The
                      structuring call is a separate, schema-constrained LLM
                      extraction: its prompt guarantees it won't invent facts,
                      but nothing guarantees it captures every fact in the
                      answer. Hiding the prose risked silently dropping
                      content the fixed SpellContent/StatBlockContent schema
                      has no field for. */}
                  {exchange.response.spell_content && (
                    <SpellCard {...toSpellCardProps(exchange.response.spell_content)} density="default" />
                  )}
                  {exchange.response.stat_block && (
                    <StatBlockCard {...toStatBlockCardProps(exchange.response.stat_block)} density="default" />
                  )}

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
      </div>

      {/* Jump-to-latest — only while the reader has scrolled away (pp6q.1.3).
          A real <button> rather than a floating decoration so it is keyboard
          reachable and announced, like the ChatGPT/Claude equivalent. */}
      {!atBottom && exchanges.length > 0 && (
        <div className="chat-pane__jump">
          <button
            type="button"
            className="chat-pane__jump-button"
            onClick={scrollToLatest}
          >
            <span className="material-symbols-rounded" aria-hidden="true">arrow_downward</span>
            Jump to latest
          </button>
        </div>
      )}

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
