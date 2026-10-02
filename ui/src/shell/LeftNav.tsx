/**
 * LeftNav — Fixed sidebar navigation.
 *
 * Brand logo, mode selection chips, conversation list, and UserMenu.
 *
 * In the GM channel with a campaign in any state (agent-forge-harness-1kg.2.5,
 * PR-2; brief section 7.7 and its Critic's item 5) the conversation list is the
 * campaign's: a campaign line, then that campaign's GM threads from the
 * server. Every other case renders exactly the markup it always has.
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import { Chip } from '../ds/Chip'
import { IconButton } from '../ds/IconButton'
import { useAppNav } from './AppNav'
import { UserMenu } from './UserMenu'
import { useConversationStore } from './ConversationStoreContext'
import { useCurrentUser } from './currentUser'
import type { Conversation } from './conversationStore'
import { modesForRole, accentClass } from './modes'
import { useCampaign } from './campaignContext'
import { LibraryNavGroup } from './LibraryNavGroup'
import { useCanvasMounted, useWorkbenchActive } from './canvasContext'
import { useCampaignThreads } from './campaignThreads'
import { PendingButton, useRetrying } from './CampaignPicker'
import './LeftNav.css'
import './modeAccents.css'

// Copy (I-18, critic 5): constants `cub` may replace.
const CAMPAIGN_LOADING = 'Loading campaign…'
const CAMPAIGN_FAILED = "Couldn't load campaigns"
const CAMPAIGN_UNAVAILABLE = "That campaign isn't available."
const CONTINUE_WITHOUT = 'Continue without a campaign'
const THREADS_LOADING = 'Loading conversations…'
const THREADS_FAILED = "Couldn't load conversations"
const RENAME_INVALID = 'A title is 1 to 200 characters on one line.'
const RENAME_FAILED = "Couldn't rename the conversation."
// Copy (agent-forge-harness-74j): the tavern entry, in every selection state.
const CHOOSE_CAMPAIGN = 'Choose a campaign'
const SWITCH_CAMPAIGN = 'Switch campaign'

export interface LeftNavProps {
  /** Called AFTER a navigation action: a mode chip, a conversation button,
   * New conversation. Never for the rename button, the rename input, or
   * anything in `settings` or the footer (agent-forge-harness-0rn: the
   * narrow drawer closes on navigation only, INF-8). */
  onNavigate?: () => void
  /** Rendered between the conversation list and the footer (the narrow
   * layout's NavSettings). */
  settings?: React.ReactNode
  /** Called when a Campaign Library row opened the panel, just before `onNavigate`: the shell hands focus to the panel as the drawer closes. */
  onOpenLibrary?: () => void
}

export function LeftNav({ onNavigate, settings, onOpenLibrary }: LeftNavProps): React.JSX.Element {
  const { mode, setMode, conversationId, setConversationId, openTavern } = useAppNav()
  const { user } = useCurrentUser()
  const store = useConversationStore()
  const { selection, enabled } = useCampaign()
  // The Campaign Library group is the Workbench's, so it needs a canvas to open into (a bare LeftNav has none).
  const workbenchActive = useWorkbenchActive()
  const canvasMounted = useCanvasMounted()
  const workbench = workbenchActive && canvasMounted
  const campaignGm = mode === 'gm' && selection.kind !== 'none'
  const [renameState, setRenameState] = React.useState<{
    id: string
    title: string
  } | null>(null)
  const convs: Conversation[] = store.list(mode)

  const handleNew = React.useCallback(() => {
    const conv = store.create(mode)
    setConversationId(conv.id)
    onNavigate?.()
  }, [mode, store, setConversationId, onNavigate])

  const saveRename = React.useCallback((id: string) => {
    if (renameState?.id !== id) return
    store.rename(id, renameState.title)
    setRenameState(null)
  }, [renameState, store])

  return (
    <nav className="left-nav" aria-label="Main navigation">
      {/* Brand lives once in the TopBar header (swe1.10) — the sidebar starts
          straight at the mode chips to avoid a duplicate 'Aetheril'. */}

      {/* Mode chips */}
      <div className="left-nav__modes">
        {modesForRole(user.role).map(({ mode: m, icon, label }) => (
          <Chip
            key={m}
            type="filter"
            icon={icon}
            label={label}
            selected={mode === m}
            onClick={() => {
              setMode(m)
              onNavigate?.()
            }}
            className={`left-nav__mode-chip ${accentClass(m)}`}
          />
        ))}
      </div>

      {mode === 'gm' && enabled && selection.kind === 'none' && (
        <div className="left-nav__campaign">
          <Button variant="text" onClick={() => {
            openTavern?.()
            onNavigate?.()
          }}>{CHOOSE_CAMPAIGN}</Button>
        </div>
      )}
      {campaignGm ? <CampaignConversations onNavigate={onNavigate} /> : (
      /* Conversation list */
      <div className="left-nav__conversations">
        <div className="left-nav__conversations-header">
          <p className="left-nav__conversations-title">Conversations</p>
          <IconButton
            icon="add"
            ariaLabel="New conversation"
            size="small"
            onClick={handleNew}
          />
        </div>

        {convs.map((conv) => {
          const isRenaming = renameState?.id === conv.id
          return (
            <div className="left-nav__conversation-row" key={conv.id}>
              {isRenaming ? (
                <input
                  className="left-nav__conversation-input"
                  aria-label={`Conversation title for ${conv.title}`}
                  value={renameState.title}
                  autoFocus
                  onChange={(event) => {
                    setRenameState({ id: conv.id, title: event.target.value })
                  }}
                  onBlur={() => saveRename(conv.id)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      saveRename(conv.id)
                    } else if (event.key === 'Escape') {
                      event.preventDefault()
                      setRenameState(null)
                    }
                  }}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setConversationId(conv.id)
                    onNavigate?.()
                  }}
                  aria-pressed={conversationId === conv.id}
                  className={
                    conversationId === conv.id
                      ? 'left-nav__conversation left-nav__conversation--selected'
                      : 'left-nav__conversation'
                  }
                >
                  {conv.title}
                </button>
              )}
              <IconButton
                icon="edit"
                ariaLabel={`Rename ${conv.title}`}
                size="small"
                className="left-nav__conversation-rename"
                onClick={() => {
                  setRenameState({ id: conv.id, title: conv.title })
                }}
              />
            </div>
          )
        })}
      </div>
      )}

      {/* 1kg.6.4: a GM's way to the Campaign Library, whose panel holds the lists. */}
      {workbench && <LibraryNavGroup onNavigate={onNavigate} onOpenLibrary={onOpenLibrary} />}

      {settings}

      {/* Bottom row: UserMenu */}
      <div className="left-nav__footer">
        <UserMenu />
      </div>
    </nav>
  )
}

interface RenameDraft {
  readonly id: string
  readonly title: string
  readonly busy: boolean
  readonly error: 'invalid' | 'failed' | null
}

/** The GM channel's list while a campaign is chosen, loading, failed or gone
 * (section 7.7, critic 5). Retry, Load more, rename and Continue without a
 * campaign never call `onNavigate`; choosing a thread and New conversation do. */
function CampaignConversations({ onNavigate }: Pick<LeftNavProps, 'onNavigate'>): React.JSX.Element {
  const { conversationId, setConversationId, openTavern } = useAppNav()
  const { selection, retrySelection, clearCampaign } = useCampaign()
  const threads = useCampaignThreads()
  const line = React.useRef<HTMLParagraphElement>(null)
  const header = React.useRef<HTMLParagraphElement>(null)
  const errorId = React.useId()
  const [selectionRetry, pressSelectionRetry] = useRetrying(selection.kind === 'failed', selection.kind === 'restoring')
  const [threadRetry, pressThreadRetry] = useRetrying(threads.status === 'failed', threads.status === 'loading')
  const [rename, setRename] = React.useState<RenameDraft | null>(null)

  const text = selection.kind === 'selected'
    ? `Campaign: ${selection.campaign.name}`
    : selection.kind === 'unavailable' ? CAMPAIGN_UNAVAILABLE
      : selection.kind === 'failed' ? CAMPAIGN_FAILED : CAMPAIGN_LOADING

  const leave = (): void => {
    // The list this line heads goes away: focus lands on the GM chip.
    const nav = line.current?.closest('nav')
    void clearCampaign().then((outcome) => {
      if (outcome === 'switched') nav?.querySelector<HTMLElement>('.left-nav__modes [aria-pressed="true"]')?.focus()
    })
  }

  const save = (): void => {
    if (rename === null || rename.busy) return
    const { id, title } = rename
    setRename({ ...rename, busy: true })
    void threads.rename(id, title).then((outcome) => {
      setRename((now) => {
        if (now?.id !== id) return now
        return outcome === 'renamed' || outcome === 'gone' ? null : { ...now, busy: false, error: outcome }
      })
    })
  }

  return (
    <>
      <div className="left-nav__campaign">
        <p ref={line} tabIndex={-1} className="left-nav__campaign-line">{text}</p>
        {selectionRetry && (
          <PendingButton busy={selection.kind === 'restoring'} landing={line} onPress={() => {
            pressSelectionRetry()
            retrySelection()
          }}>Retry</PendingButton>
        )}
        {(selection.kind === 'failed' || selection.kind === 'unavailable') && (
          <Button variant="text" onClick={leave}>{CONTINUE_WITHOUT}</Button>
        )}
        <Button variant="text" onClick={() => {
          openTavern?.()
          onNavigate?.()
        }}>{selection.kind === 'selected' ? SWITCH_CAMPAIGN : CHOOSE_CAMPAIGN}</Button>
      </div>

      {selection.kind === 'selected' && (
        <div className="left-nav__conversations">
          <div className="left-nav__conversations-header">
            <p ref={header} tabIndex={-1} className="left-nav__conversations-title">Conversations</p>
            <IconButton icon="add" ariaLabel="New conversation" size="small" onClick={() => {
              setConversationId(null)
              onNavigate?.()
            }} />
          </div>

          {threads.threads.map((row) => (rename?.id === row.id ? (
            <div className="left-nav__conversation-row left-nav__conversation-row--editing" key={row.id}>
              <input
                className="left-nav__conversation-input"
                aria-label={`Conversation title for ${row.title}`}
                aria-invalid={rename.error === 'invalid' || undefined}
                aria-describedby={rename.error === null ? undefined : errorId}
                value={rename.title}
                autoFocus
                onChange={(event) => setRename({ ...rename, title: event.target.value })}
                onBlur={() => {
                  if (rename.error === null) save()
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    save()
                  } else if (event.key === 'Escape') {
                    event.preventDefault()
                    setRename(null)
                  }
                }}
              />
              {rename.error !== null && (
                <p id={errorId} className="left-nav__campaign-error">
                  {rename.error === 'invalid' ? RENAME_INVALID : RENAME_FAILED}
                </p>
              )}
              {rename.error === 'failed' && <PendingButton busy={rename.busy} landing={header} onPress={save}>Retry</PendingButton>}
            </div>
          ) : (
            <div className="left-nav__conversation-row" key={row.id}>
              <button
                type="button"
                onClick={() => {
                  setConversationId(row.id)
                  onNavigate?.()
                }}
                aria-pressed={conversationId === row.id}
                className={conversationId === row.id ? 'left-nav__conversation left-nav__conversation--selected' : 'left-nav__conversation'}
              >
                {row.title}
              </button>
              <IconButton
                icon="edit"
                ariaLabel={`Rename ${row.title}`}
                size="small"
                className="left-nav__conversation-rename"
                onClick={() => setRename({ id: row.id, title: row.title, busy: false, error: null })}
              />
            </div>
          )))}

          {threads.status === 'loading' && (
            <>
              <div className="left-nav__skeleton" aria-hidden="true" />
              <div className="left-nav__skeleton" aria-hidden="true" />
              <p className="left-nav__campaign-note">{THREADS_LOADING}</p>
            </>
          )}
          {threadRetry && (
            <div className="left-nav__campaign">
              <p className="left-nav__campaign-note">{THREADS_FAILED}</p>
              <PendingButton busy={threads.status === 'loading'} landing={header} onPress={() => {
                pressThreadRetry()
                threads.retry()
              }}>Retry</PendingButton>
            </div>
          )}
          {threads.hasMore && !threadRetry && (
            <PendingButton busy={threads.status === 'loading'} landing={header} onPress={threads.loadMore}>Load more</PendingButton>
          )}
        </div>
      )}
    </>
  )
}
