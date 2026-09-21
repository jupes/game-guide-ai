/**
 * ToolComposer (1kg.3.3) — the rail, the slash menu, More and the composer as
 * one assembly, with the record's single invocation path running through it.
 *
 * The rule the whole task turns on (X-1, RAIL-1): **a tap arms, only a submit
 * runs.** Every surface here — a rail tap, a More pick, a slash-menu pick —
 * ends in `armDraft`, which writes text into the draft and nothing else. The
 * one place that can produce an invocation is `submitDraft`, reached from Enter
 * or Send. There is deliberately no `runTool` prop for a surface to call.
 *
 * Scope: this is a controlled building block. It takes the draft, the pins and
 * the capability answer, and reports what the GM asked for. Sending an
 * invocation, the assistant lane, hydration and the canvas are `1kg.4.5`'s.
 */

import * as React from 'react'
import { IconButton } from '../ds/IconButton'
import { TextField } from '../ds/TextField'
import { BRIEF_MAX_CHARS } from './contracts'
import type { ToolId } from './contracts'
import type { Tool, ToolAvailability } from './registry'
import { CustomiseRailDialog } from './CustomiseRailDialog'
import { SlashMenu } from './SlashMenu'
import { ToolRail } from './ToolRail'
import { useSlashMenu } from './useSlashMenu'
import { ARMED_HINT, armDraft, armedTool, briefCounterMessage, briefLength, parseDraft, submitDraft } from './slash'
import type { Submission } from './slash'
import './ToolComposer.css'

/** RAIL-16: the only thing that makes Send unavailable besides a pending chat turn. */
export const WAITING_MESSAGE = 'Waiting for a tool to finish…'

export interface ToolInvocation {
  tool: Tool
  /** Trimmed, 0–2,000 code points, already checked against the tool's policy (RAIL-5, RAIL-6). */
  brief: string
}

export interface ToolComposerProps {
  /** RAIL-11: the rail's pins, in order. */
  pins: readonly ToolId[]
  /** SLASH-15's Save. */
  onPinsChange: (pins: ToolId[]) => void
  /** RAIL-10, from `toolAvailability`. */
  availability: Readonly<Record<ToolId, ToolAvailability>>
  /** AE-58: the capability lookup failed. */
  capabilitiesFailed?: boolean
  onRetryCapabilities?: () => void
  /** RAIL-13 / SLASH-3. The campaign model arrives with `1kg.2.5`. */
  campaignSelected?: boolean
  onChooseCampaign?: () => void
  /** RAIL-25: the draft belongs to its conversation — `useConversationDrafts` owns it. */
  draft: string
  onDraftChange: (draft: string) => void
  /** The one billable outcome, and only from an explicit submit (X-1). */
  onInvoke: (invocation: ToolInvocation) => void
  /** RAIL-14: a plain GM message is still an ordinary chat turn. */
  onChat: (text: string) => void
  /** RAIL-16: today's one-at-a-time `/chat` rule. */
  chatPending?: boolean
  /** RAIL-16: the client knows the Workbench cap is reached (X-5). */
  workbenchBusy?: boolean
  /** CANVAS-22: slash parsing is off while an edit is armed. */
  slashEnabled?: boolean
  placeholder?: string
  hint?: string | null
  /** SLASH-12: the live region is debounced so arrowing does not flood it. */
  announceDelayMs?: number
  className?: string
}

export function ToolComposer({
  pins,
  onPinsChange,
  availability,
  capabilitiesFailed = false,
  onRetryCapabilities,
  campaignSelected = true,
  onChooseCampaign,
  draft,
  onDraftChange,
  onInvoke,
  onChat,
  chatPending = false,
  workbenchBusy = false,
  slashEnabled = true,
  placeholder = 'Ask…',
  hint,
  announceDelayMs = 150,
  className,
}: ToolComposerProps): React.JSX.Element {
  const rootRef = React.useRef<HTMLDivElement>(null)
  const fieldRef = React.useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  const caretToEnd = React.useRef(false)
  const [focused, setFocused] = React.useState(false)
  const [blocked, setBlocked] = React.useState<Extract<Submission, { kind: 'blocked' }> | null>(null)
  const [customising, setCustomising] = React.useState(false)

  const parse = React.useMemo(() => parseDraft(draft, { slashEnabled }), [draft, slashEnabled])
  const armed = armedTool(parse)

  const setDraft = React.useCallback(
    (next: string) => {
      // Any edit retires the last refusal: the message described a draft that
      // no longer exists (STATE-2 — an error never outlives its cause).
      setBlocked(null)
      onDraftChange(next)
    },
    [onDraftChange],
  )

  /** RAIL-1 and table 3.2 — the one thing every surface does. */
  const arm = React.useCallback(
    (tool: Tool) => {
      caretToEnd.current = true
      setDraft(armDraft(draft, tool))
      fieldRef.current?.focus()
    },
    [draft, setDraft],
  )

  const menu = useSlashMenu({ parse, availability, onAccept: arm, focused })

  // RAIL-1: the caret sits at the end of the armed draft, so the next keystroke
  // is the brief. Layout effect, so the caret never flashes at position 0.
  React.useLayoutEffect(() => {
    if (!caretToEnd.current) return
    caretToEnd.current = false
    const field = fieldRef.current
    if (!field) return
    field.focus()
    field.setSelectionRange(draft.length, draft.length)
  }, [draft])

  // SLASH-8: an outside click closes the menu. Blur covers most of it; this
  // also catches a press on something that never takes focus.
  React.useEffect(() => {
    if (!menu.open) return
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) menu.dismiss()
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [menu])

  // SLASH-12: a plain textbox announces its active descendant unreliably, so a
  // debounced polite region says the count and the active option out loud.
  const [announcement, setAnnouncement] = React.useState('')
  React.useEffect(() => {
    const handle = window.setTimeout(() => setAnnouncement(menu.announcement), announceDelayMs)
    return () => window.clearTimeout(handle)
  }, [menu.announcement, announceDelayMs])

  const submit = React.useCallback(() => {
    const outcome = submitDraft(draft, { availability, campaignSelected, slashEnabled })
    switch (outcome.kind) {
      case 'none':
        return
      case 'chat':
        setBlocked(null)
        onChat(outcome.text)
        onDraftChange('')
        return
      case 'invoke':
        setBlocked(null)
        onInvoke({ tool: outcome.tool, brief: outcome.brief })
        onDraftChange('')
        return
      case 'blocked':
        // SLASH-4: the draft stays. Nothing is sent, and nothing is discarded.
        setBlocked(outcome)
    }
  }, [availability, campaignSelected, draft, onChat, onDraftChange, onInvoke, slashEnabled])

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      if (menu.handleKeyDown(event)) return
      if (event.nativeEvent.isComposing) return
      // Enter sends, Shift+Enter is a newline — today's composer contract.
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        submit()
      }
    },
    [menu, submit],
  )

  const length = briefLength(parse)
  const overLength = length > BRIEF_MAX_CHARS
  const sendUnavailable = chatPending || workbenchBusy
  const message = overLength ? briefCounterMessage(length) : workbenchBusy ? WAITING_MESSAGE : (blocked?.message ?? null)

  return (
    <div className={['gm-composer', className].filter(Boolean).join(' ')} ref={rootRef}>
      <ToolRail
        pins={pins}
        availability={availability}
        onArm={arm}
        onCustomise={() => setCustomising(true)}
        campaignSelected={campaignSelected}
        onChooseCampaign={onChooseCampaign}
        capabilitiesFailed={capabilitiesFailed}
        onRetryCapabilities={onRetryCapabilities}
        {...(hint === undefined ? {} : { hint })}
      />

      {menu.open && (
        <SlashMenu
          id={menu.listboxId}
          token={menu.token}
          options={menu.options}
          availability={availability}
          activeIndex={menu.activeIndex}
          optionId={menu.optionId}
          onPick={menu.pick}
          onHover={menu.setActiveIndex}
          className="gm-composer__menu"
        />
      )}

      {/* RAIL-3: status text, not a control. It never offers to run anything —
          pressing Enter is what runs a tool. */}
      {armed && (
        <div className="gm-composer__armed" role="status">
          <span className="material-symbols-rounded gm-composer__armed-icon" aria-hidden="true">
            {armed.icon}
          </span>
          <span className="gm-composer__armed-label">{armed.label}</span>
          <span className="gm-composer__armed-blurb">{armed.blurb}</span>
          <span className="gm-composer__armed-hint">{ARMED_HINT}</span>
        </div>
      )}

      {message && (
        <p className="gm-composer__message" role="status">
          {message}
        </p>
      )}

      <div className="gm-composer__row">
        {/* The composer IS a combobox — it has had `aria-autocomplete="list"`,
            an `aria-controls` popup and `aria-activedescendant` since 1kg.3.3 —
            but it never said so, so assistive tech was never told a popup
            existed or whether it was open. (SLASH-12's debounced live region
            was the workaround for exactly that gap.) The role goes on a wrapper
            rather than on the field because `role="combobox"` is not permitted
            on `<textarea>`; that is the ARIA 1.1 shape, and it is the only one
            available to a MULTILINE autocomplete. `aria-controls` is set only
            while the menu is open, so it never dangles at a missing id. */}
        <div
          className="gm-composer__combobox"
          role="combobox"
          aria-label="Message"
          aria-expanded={menu.open}
          aria-haspopup="listbox"
          {...(menu.open ? { 'aria-controls': menu.listboxId } : {})}
        >
          <TextField
            multiline
            autoGrow
            rows={1}
            fullWidth
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            placeholder={placeholder}
            aria-label="Message"
            aria-autocomplete="list"
            {...(menu.open ? { 'aria-controls': menu.listboxId } : {})}
            {...(menu.activeOptionId ? { 'aria-activedescendant': menu.activeOptionId } : {})}
            aria-invalid={overLength || undefined}
            ref={fieldRef}
          />
        </div>
        <IconButton
          icon="send"
          // RAIL-3: the Send button says what it will run.
          ariaLabel={armed ? `Run ${armed.label} tool` : 'Send message'}
          onClick={submit}
          disabled={sendUnavailable || draft.trim() === ''}
        />
      </div>

      {/* SLASH-12's live region. Polite, debounced, and the only thing that
          announces the menu — the options themselves are never focused. */}
      <p className="gm-composer__sr-only" role="status" aria-live="polite">
        {announcement}
      </p>

      <CustomiseRailDialog
        open={customising}
        pins={pins}
        availability={availability}
        onSave={(next) => {
          setCustomising(false)
          onPinsChange(next)
        }}
        onCancel={() => setCustomising(false)}
      />
    </div>
  )
}
