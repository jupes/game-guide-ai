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
import { StatBlockCard } from '../ds/StatBlockCard'
import { SourceList } from '../components/SourceList'
import { Markdown } from '../components/Markdown'
import { CHAT_TEXT_MAX_CHARS, codePointLength } from '../gm/contracts'
import { useChat } from '../useChat'
import { exportChat } from '../exportChat'
import { toSpellCardProps, toStatBlockCardProps } from '../gm/adapters'
import { GmThread } from '../gm/GmThread'
import { exchangesForExport, turnFromExchange, turnsFromTimeline, useGmTimeline } from '../gm/gmTimeline'
import type { LoadTimelinePageFn } from '../gm/gmTimeline'
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
  Suggestion,
  UploadAttachmentResult,
} from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'
import './ChatPane.css'

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

// ── Single-live-region announcer (agent-forge-harness-4oz) ──────────────────
// The exact phrase announced the moment a turn is SENT — asserted verbatim in
// ChatPane.test.tsx and ChatPane.stories.tsx > AwaitingAnswer, so it lives in
// one named place rather than as a string literal repeated at each call site.
const PENDING_ANNOUNCEMENT = 'Consulting the tomes…'

// agent-forge-harness-764: the composer's own bound, mirroring the server's
// CHAT_TEXT_MAX_CHARS gate in service/app.py::chat() (same constant, same
// ceiling, checked before any provider work happens). Counted in code points
// like ToolComposer's BRIEF_MAX_CHARS counter (`briefCounterMessage`) — not
// `.length`, which counts UTF-16 units and would undercount astral characters
// (see ui/src/gm/documentFields.ts and DocumentField.test.tsx).
function chatPromptCounterMessage(length: number): string {
  return `${length} of ${CHAT_TEXT_MAX_CHARS} characters — shorten your message to send it.`
}

// ── Component ─────────────────────────────────────────────────────────────────

/** 1kg.3.4: the GM channel's history is the typed timeline, not `/messages`. */
const SKIP_RECALL: LoadHistoryFn = async () => ({ kind: 'ok', messages: [] })

export interface ChatPaneProps {
  post?: PostFn
  loadHistory?: LoadHistoryFn
  /** The GM channel's history (1kg.3.4). */
  loadTimeline?: LoadTimelinePageFn
  uploadAttachment?: UploadAttachmentFn
  getAttachments?: GetAttachmentsFn
}

/** Which side of the GM boundary a pane is on: the GM channel reads the typed
 * timeline, every other channel reads `/messages`. */
type Side = 'gm' | 'chat'

export function ChatPane(props: ChatPaneProps): React.JSX.Element {
  // 1kg.3.4: the GM channel and the others read history from different
  // sources, so crossing between them remounts the pane. Turns one source
  // already holds are then never drawn again beside the other's copy of them,
  // and a GM draft is never carried into another channel (RAIL-25).
  //
  // Never under a turn in flight, though: a remount would drop it — its answer
  // would never land, and the composer would unlock beside it. The pane holds
  // the side the turn was sent from until it settles, so the answer lands
  // there and is never drawn in the other side's lanes; then it crosses, and
  // the new side reads a history that now holds the turn.
  const { mode } = useAppNav()
  const [held, setHeld] = React.useState<Side | null>(null)
  const side: Side = held ?? (mode === 'gm' ? 'gm' : 'chat')
  const holdWhilePending = React.useCallback((pending: boolean) => setHeld(pending ? side : null), [side])
  return <ChatPaneBody key={side} {...props} side={side} onPendingChange={holdWhilePending} />
}

interface ChatPaneBodyProps extends ChatPaneProps {
  /** The mode's side of the GM boundary, or the side a turn in flight was sent from. */
  side: Side
  onPendingChange: (pending: boolean) => void
}

function ChatPaneBody({
  side,
  onPendingChange,
  post,
  loadHistory,
  loadTimeline,
  uploadAttachment = defaultUploadAttachment,
  getAttachments = defaultGetAttachments,
}: ChatPaneBodyProps): React.JSX.Element {
  const { mode, conversationId, setConversationId } = useAppNav()
  const gm = side === 'gm'
  const conversationStore = useConversationStore()
  // agent-forge-harness-bta: the conversation's bound preference (a public
  // /models id per D-9, or, for a conversation started before D-9, the exact
  // alias it was already bound with — service/app.py's `_pre_d9_binding`,
  // agent-forge-harness-a6o/#122). Threaded through to `useChat` unchanged —
  // never rewritten or filtered here — so it is exactly what ModelPicker
  // shows and exactly what the server already bound this conversation to.
  const conversation = conversationId !== null ? conversationStore.get(conversationId) : undefined
  // agent-forge-harness-ekf / agent-forge-harness-4oz: the ONE announcer for
  // the whole pane — 4oz folded the pending announcement into this same node
  // (see its comment below) rather than leaving a second, per-exchange
  // `role="status"` span inside the transcript. Set to PENDING_ANNOUNCEMENT
  // when a turn is sent (handleSend, below) and to the settle outcome via
  // useChat's onTurnSettled seam — never from a recall, a conversation
  // switch or a re-render.
  const [arrival, setArrival] = React.useState('')
  const handleTurnSettled = React.useCallback(
    (outcome: 'done' | 'error', _sentFor: string | null, shown: boolean) => {
      // agent-forge-harness-swg (pr114 M-1, pr129 M-1): this component is
      // never remounted on a conversation switch (see the comment on the
      // transcript region below), so a turn can settle after the user left
      // its conversation — or left and came back, by which time a recall has
      // replaced the exchange it would have filled. Announce an outcome only
      // when useChat reports the settle as SHOWN: applied to the exchanges on
      // screen and drawn there. Comparing conversation ids is not enough.
      if (shown) {
        setArrival(outcome === 'done' ? 'Answer received' : 'Answer failed')
        return
      }
      // pr129 M-2: a suppressed settle must not leave its turn's pending
      // phrase standing — the next send would set the SAME text, the live
      // region would not change, and that send's pending state would go
      // unannounced. Clearing to empty is itself silent (a removal from a
      // live region is not announced). Any other text is left alone.
      setArrival((current) => (current === PENDING_ANNOUNCEMENT ? '' : current))
    },
    [],
  )
  const { exchanges, send, pending, historyError, loadingHistory } = useChat({
    post,
    loadHistory: gm ? SKIP_RECALL : loadHistory,
    mode,
    conversationId,
    modelPreference: conversation?.modelPreference,
    onConversationAdopted: setConversationId,
    onTurnSettled: handleTurnSettled,
  })
  // Keeps ChatPane on this side of the GM boundary while a turn is in flight.
  React.useEffect(() => {
    onPendingChange(pending)
  }, [onPendingChange, pending])
  // 1kg.3.4: in the GM channel a stored entry and a live turn become the same
  // GmTurn, so a reload draws an answer exactly as it arrived. The thread's
  // empty, loading and error states are §12.2's, which are today's.
  const timeline = useGmTimeline(conversationId, gm, loadTimeline)
  const gmTurns = React.useMemo(
    () => (gm ? [...turnsFromTimeline(timeline.items), ...exchanges.map(turnFromExchange)] : []),
    [gm, timeline.items, exchanges],
  )
  const threadError = gm ? timeline.error : historyError
  const threadLoading = gm ? timeline.loading : loadingHistory
  const threadLength = gm ? gmTurns.length : exchanges.length
  const [draft, setDraft] = React.useState('')
  // agent-forge-harness-764: block a submit before it ever reaches the wire,
  // mirroring the server-side gate in service/app.py::chat().
  const draftLength = codePointLength(draft)
  const overLength = draftLength > CHAT_TEXT_MAX_CHARS
  const counterId = React.useId()
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

  // Load earlier (1kg.3.6): the feed's height just before a press, captured
  // synchronously in the click handler — before React has re-rendered for
  // either the loading state or the prepended turns. The layout effect below
  // turns that into a scrollTop adjustment once the older turns land, so the
  // content the reader was looking at holds still while the thread above it
  // grows. `null` once consumed, and reset on a conversation switch so a
  // press that never resolved before the switch cannot misapply itself to a
  // different conversation's geometry.
  const earlierScrollAdjustRef = React.useRef<number | null>(null)
  // The other half of STATE-7's pair (below): whether the settle-announcement
  // effect just saw a walk that was THIS conversation's, so switching away
  // mid-walk cannot fire a stale "loaded"/"failed" phrase once the NEW
  // conversation's own (unrelated) loadingEarlier happens to read false.
  const wasLoadingEarlierRef = React.useRef(false)
  React.useEffect(() => {
    earlierScrollAdjustRef.current = null
    wasLoadingEarlierRef.current = false
  }, [conversationId])

  React.useLayoutEffect(() => {
    const feed = feedRef.current
    const before = earlierScrollAdjustRef.current
    earlierScrollAdjustRef.current = null
    if (feed && before !== null) feed.scrollTop += feed.scrollHeight - before
  }, [timeline.items])

  const { loadEarlier } = timeline
  const handleLoadEarlier = React.useCallback(() => {
    const feed = feedRef.current
    if (feed) earlierScrollAdjustRef.current = feed.scrollHeight
    // agent-forge-harness-ekf / agent-forge-harness-4oz: the same single
    // announcer, not a live region of GmThread's own (STATE-7 rations this
    // to one announcement now and one when the walk settles, below).
    setArrival('Loading earlier turns…')
    loadEarlier()
  }, [loadEarlier])

  // The other half of STATE-7's pair: once a Load earlier walk settles,
  // announce how it went. Keyed on the loadingEarlier→settled transition so
  // this never fires on mount or from an unrelated rerender.
  React.useEffect(() => {
    if (wasLoadingEarlierRef.current && !timeline.loadingEarlier) {
      setArrival(timeline.earlierError !== null ? 'Couldn’t load earlier turns' : 'Earlier turns loaded')
    }
    wasLoadingEarlierRef.current = timeline.loadingEarlier
  }, [timeline.loadingEarlier, timeline.earlierError])

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
  }, [exchanges, conversationId, timeline.items])

  const handleSend = React.useCallback(() => {
    const trimmed = draft.trim()
    if (!trimmed || pending || overLength) return
    if (conversationId !== null) {
      conversationStore.recordFirstPrompt(conversationId, trimmed)
    }
    // agent-forge-harness-ekf / agent-forge-harness-4oz: nothing else ever
    // changes the announcer — not a recall, not a conversation switch — only
    // sending the NEXT turn, which re-announces the pending phrase on this
    // SAME already-mounted node (never a fresh node mounted with its text
    // already inside it — that shape is the defect 4oz fixed).
    setArrival(PENDING_ANNOUNCEMENT)
    send(trimmed)
    setDraft('')
  }, [conversationId, conversationStore, draft, overLength, pending, send])

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
      {/* agent-forge-harness-27h: the feed is the scroller, and until the
          shell got stories nothing in it was focusable — so a keyboard-only
          reader could not scroll back through their own conversation at all
          (axe `scrollable-region-focusable`, WCAG 2.1.1). It only escaped
          notice because an answer WITH citations happens to contain a
          focusable <summary>; a recalled history has none.

          `tabIndex={0}` puts the transcript in the tab order, where PageUp,
          PageDown and the arrow keys scroll it, and `role="region"` +
          `aria-label` give that new stop the name a focusable region needs, so
          assistive tech announces it as something rather than as a bare group.

          Rework 1 — this was briefly `role="log"`, and that was broader than
          the defect. `log` carries an implicit `aria-live="polite"` over
          EVERYTHING inside it, the user's own prompts included; and
          WorkspaceShell mounts ChatPane with no `key` while useChat replaces
          `exchanges` in place (its effect keys on `conversationId`), so
          switching conversations mutates the live region rather than
          remounting it — a recalled 40-turn history arriving as "new" content.
          Both the pending state AND the resolution of a turn are announced
          (agent-forge-harness-ekf / agent-forge-harness-4oz) by the single,
          persistent `role="status"` node BELOW this transcript
          (`.chat-pane__arrival`), never by this region itself and never by a
          second node inside it.

          Axe has no rule for any of this, in either direction, so
          ChatPane.stories.tsx > TranscriptIsANamedRegionNotALiveRegion pins
          the role by name. */}
      <div
        className="chat-pane__exchanges aether-parchment"
        ref={feedRef}
        onScroll={handleFeedScroll}
        role="region"
        aria-label="Conversation"
        tabIndex={0}
      >
        <div className="chat-pane__column">
        {/* History recall failed — recoverable: the thread starts empty. */}
        {threadError && <ChatMessage role="system">{threadError}</ChatMessage>}

        {threadLength === 0 && threadLoading ? (
          // agent-forge-harness-swg (pr116 M-1): NOT a live region. This node
          // used to carry `role="status"` mounted together with its own
          // text — a SECOND live region alongside `.chat-pane__arrival`
          // below, which is exactly the shape agent-forge-harness-4oz exists
          // to rule out (see the comment on the transcript region above and
          // on `.chat-pane__arrival` below). A recall is visible, sighted
          // text; the pane's one live region stays silent for it, same as
          // for a conversation switch (E6 in ChatPane.test.tsx: "a recall
          // announces nothing"). Applies on both sides of the GM boundary —
          // `threadLoading` is `timeline.loading` on the GM side (1kg.3.4).
          <p className="chat-pane__empty">
            Recalling the conversation…
          </p>
        ) : threadLength === 0 ? (
          !threadError && <p className="chat-pane__empty">{EMPTY_LABELS[mode]}</p>
        ) : gm ? (
          <GmThread
            turns={gmTurns}
            hasEarlier={timeline.hasEarlier}
            loadingEarlier={timeline.loadingEarlier}
            earlierError={timeline.earlierError}
            onLoadEarlier={handleLoadEarlier}
          />
        ) : (
          exchanges.map((exchange) => (
            <React.Fragment key={exchange.id}>
              {/* Player prompt */}
              <ChatMessage role="player">{exchange.prompt}</ChatMessage>

              {/* DM response */}
              {exchange.status === 'pending' && (
                <ChatMessage role="dm">
                  {/* Purely decorative (aria-hidden) — the pending state is
                      announced once for the whole pane, by the single
                      `.chat-pane__arrival` live region below, not by a node
                      here. A second, per-exchange `role="status"` span used
                      to live in this spot; keeping it would be the
                      two-live-regions-in-one-pane defect
                      agent-forge-harness-4oz exists to fix (swapping the
                      announcement for a silent animation would, in turn, be
                      an a11y regression dressed as polish — pp6q.1.5 — which
                      is why the arrival node below carries it instead). */}
                  <span className="chat-pane__typing" aria-hidden="true">
                    <i className="chat-pane__dot" />
                    <i className="chat-pane__dot" />
                    <i className="chat-pane__dot" />
                  </span>
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

      {/* agent-forge-harness-ekf / agent-forge-harness-4oz — the ONE
          announcer for the whole pane. A SIBLING of the transcript above,
          never inside `region "Conversation"`: the transcript stays a named,
          focusable region and NOT a live region (see the comment on it
          above). This node is mounted for the whole life of the pane — it is
          never conditionally rendered, because a live region that appears
          together with its text is the defect this bead exists to fix (a
          removal from a live region is not announced, and neither is text
          that was already there the instant a node first mounted).

          agent-forge-harness-4oz folded the pending announcement into this
          same node instead of leaving a second, per-exchange `role="status"`
          span inside the transcript (the shape `ekf` shipped it in) — two
          live regions in one pane is the less reliable shape for real screen
          readers, and this node was already mounted-empty-then-filled, so it
          is the one to consolidate onto.

          Its text now changes exactly twice per turn THIS pane sent: to
          PENDING_ANNOUNCEMENT the moment the turn is SENT (`handleSend`,
          above), and to the settle outcome the moment the turn SETTLES
          (`handleTurnSettled`, above) — or, for a settle that is never shown
          (the user left its conversation), silently back to empty instead
          (agent-forge-harness-swg, pr129 M-2). Apart from Load earlier
          (below), nothing else ever changes it — not a history recall, not a
          conversation switch. Shape copied from `gm/ToolComposer.tsx`'s own
          persistent `role="status"` node.

          1kg.3.6 (STATE-7) reuses this SAME node, the same way, for Load
          earlier: exactly twice per press, to a starting phrase in
          `handleLoadEarlier` and to the outcome once `useGmTimeline`'s
          `loadingEarlier` settles — never a second `role="status"` inside
          `GmThread` for it. */}
      <p role="status" className="chat-pane__sr-only chat-pane__arrival">
        {arrival}
      </p>

      {/* Jump-to-latest — only while the reader has scrolled away (pp6q.1.3).
          A real <button> rather than a floating decoration so it is keyboard
          reachable and announced, like the ChatGPT/Claude equivalent. */}
      {!atBottom && threadLength > 0 && (
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
          onClick={() => exportChat(gm ? [...exchangesForExport(timeline.items), ...exchanges] : exchanges)}
        />
      </div>

      {/* Composer */}
      {/* agent-forge-harness-764 × agent-forge-harness-4oz: the over-length
          counter is visible text and the field's accessible description
          (`aria-describedby`, beside `aria-invalid`), NOT a `role="status"`
          node. The single `.chat-pane__arrival` node above is this pane's one
          live region; a second one mounted together with its text is the shape
          4oz removed (and would not be reliably announced anyway). */}
      {overLength && (
        <p id={counterId} className="chat-pane__composer-message">
          {chatPromptCounterMessage(draftLength)}
        </p>
      )}
      <div className="chat-pane__composer">
        <input
          ref={fileInputRef}
          type="file"
          // Mirrors the service's ATTACHMENT_TYPES allowlist (server-side check
          // remains the source of truth; this only pre-filters the picker).
          accept=".txt,.md,.pdf"
          aria-label="Attach file"
          // agent-forge-harness-vnx: this input and the visible IconButton
          // below it used to share the accessible name "Attach file" — a
          // screen-reader user tabbing the composer met two named controls,
          // one of which does nothing on its own. `aria-hidden` takes it out
          // of the accessibility tree and `tabIndex={-1}` takes it out of the
          // tab order; the IconButton stays the only affordance. The label is
          // kept (harmlessly unreachable) so existing `getByLabelText`
          // queries keep working — Testing Library's `getByLabelText` does
          // not consult the accessibility tree.
          aria-hidden="true"
          tabIndex={-1}
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
          autoGrow
          rows={1}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask…"
          disabled={pending}
          aria-invalid={overLength || undefined}
          aria-describedby={overLength ? counterId : undefined}
          fullWidth
        />
        <IconButton
          icon="send"
          ariaLabel="Send message"
          onClick={handleSend}
          disabled={pending || draft.trim() === '' || overLength}
        />
      </div>
    </div>
  )
}
