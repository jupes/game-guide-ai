import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { documentTypeById, type DocumentType } from './registry'
import { RevealSheet, type RevealSheetProps } from './RevealSheet'
import { revealRows, type DraftAudience, type RevealEffect } from './revealFields'
import { ANA, BRANN, COLE } from './revealFixtures'

const NPC = documentTypeById('npc') as DocumentType

const DATA = {
  name: 'Ondrey',
  qualifier: 'Harbour almoner',
  voice: 'Quiet, clipped, never raised',
  tell: 'Turns the silver pin at her collar',
  wants: 'The family signet.',
  leverage: 'Her brother still draws breath in the debtors’ hall.',
}

const ROWS = revealRows(NPC, DATA)
const REVEAL: RevealEffect = { kind: 'reveal', label: 'Reveal to the table', notices: [] }

/** One stateful sheet, so ticking, choosing an audience and Cancel really move in the canvas. */
function LiveSheet(props: RevealSheetProps): React.JSX.Element {
  const [draft, setDraft] = React.useState<ReadonlySet<string>>(props.draft)
  const [audience, setAudience] = React.useState<DraftAudience>(props.audience)
  return (
    <RevealSheet
      {...props}
      draft={draft}
      audience={audience}
      onToggleRow={(row, on) => {
        const next = new Set(draft)
        for (const key of row.keys) {
          if (on) next.add(key)
          else next.delete(key)
        }
        setDraft(next)
        props.onToggleRow(row, on)
      }}
      onChooseTable={() => {
        setAudience({ kind: 'table' })
        props.onChooseTable()
      }}
      onChoosePlayers={() => {
        setAudience({ kind: 'participants', ids: [] })
        props.onChoosePlayers()
      }}
      onToggleSeat={(id, on) => {
        const current = audience.kind === 'participants' ? audience.ids : []
        setAudience({ kind: 'participants', ids: on ? [...current, id] : current.filter((seat) => seat !== id) })
        props.onToggleSeat(id, on)
      }}
    />
  )
}

const meta = {
  title: 'GM/RevealSheet',
  component: RevealSheet,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  args: {
    title: 'Ondrey',
    phase: 'ready',
    statusLine: "The table can't see this yet.",
    liveMessage: '',
    start: { pending: false, notice: null, retry: false },
    rows: ROWS,
    draft: new Set(['name', 'voice']),
    audience: { kind: 'table' },
    seats: { status: 'ready', items: [BRANN, ANA, COLE] },
    effect: REVEAL,
    live: false,
    partialAudience: false,
    staleNote: false,
    emptyDocument: false,
    conflict: false,
    error: null,
    tryAgain: false,
    stopWaiting: false,
    confirming: false,
    onStart: fn(),
    onToggleRow: fn(),
    onChooseTable: fn(),
    onChoosePlayers: fn(),
    onToggleSeat: fn(),
    onRetrySeats: fn(),
    onRetryPrepare: fn(),
    onConfirm: fn(),
    onCancel: fn(),
    onStop: fn(),
    restoreFocus: fn(),
  },
  render: (args) => <LiveSheet {...args} />,
} satisfies Meta<typeof RevealSheet>

export default meta
type Story = StoryObj<typeof meta>

/** The ready sheet over a hidden NPC: the table default ticked, a warning, an empty row and a gated picture. */
export const ReadyHidden: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('dialog', { name: 'Reveal Ondrey' })).toBeVisible()
    await expect(canvas.getByRole('switch', { name: 'Name & voice' })).toBeChecked()
    await expect(canvas.getByRole('switch', { name: 'Wants & leverage' })).toHaveAccessibleDescription('Would spoil the lie')
    await expect(canvas.getByRole('switch', { name: 'Tell' })).not.toBeChecked()
  },
}

/** Picking fields and an audience. */
export const Playground: Story = {
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('switch', { name: 'Qualifier' }))
    await expect(canvas.getByRole('switch', { name: 'Qualifier' })).toBeChecked()
    await expect(canvas.getByText('Harbour almoner')).toBeVisible()
    await userEvent.click(canvas.getByRole('radio', { name: 'Chosen players' }))
    await userEvent.click(canvas.getByRole('checkbox', { name: 'Brann' }))
    await expect(args.onToggleSeat).toHaveBeenCalled()
  },
}

export const Preparing: Story = {
  args: { phase: 'preparing' },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('Getting the document ready…')).toBeVisible()
  },
}

export const NoSession: Story = {
  args: { phase: 'no_session' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('dialog', { name: 'Start a table session' })).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Start session' })).toBeEnabled()
  },
}

export const NoSessionStarting: Story = {
  args: { phase: 'no_session', start: { pending: true, notice: null, retry: false } },
}

export const NoSessionAnotherCampaign: Story = {
  args: {
    phase: 'no_session',
    start: { pending: false, notice: 'A session is live in another campaign. End it there first.', retry: false },
  },
}

export const NoSessionRefused: Story = {
  args: {
    phase: 'no_session',
    start: { pending: false, notice: "You can't start a table session on this account.", retry: false },
  },
}

export const NoSessionFailed: Story = {
  args: { phase: 'no_session', start: { pending: false, notice: "Couldn't start a session.", retry: true } },
}

export const Unknown: Story = {
  args: { phase: 'unknown', live: true },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Reveal state unknown — reconnecting')).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Stop showing' })).toBeEnabled()
  },
}

export const EmptyDocument: Story = {
  args: {
    rows: revealRows(NPC, { name: '' }),
    draft: new Set(),
    emptyDocument: true,
    effect: { kind: 'none', label: 'Reveal', notices: [] },
  },
}

export const SealFailed: Story = { args: { phase: 'seal_failed' } }
export const DocumentRefused: Story = { args: { phase: 'refused' } }
export const DocumentUnavailable: Story = { args: { phase: 'unavailable' } }

/** Live to the table: its status, Stop showing, and Update. */
export const ReadyLive: Story = {
  args: {
    live: true,
    statusLine: 'Revealed · Name & voice · to the table',
    effect: { kind: 'none', label: 'Update', notices: ['Nothing has changed.'] },
  },
}

export const ReadyLiveStale: Story = {
  args: {
    live: true,
    staleNote: true,
    statusLine: 'Revealed · Name & voice · to the table',
    effect: { kind: 'none', label: 'Update', notices: ['Nothing has changed.'] },
  },
}

/** A live document with nothing ticked: the one Stop showing is the action. */
export const ReadyLiveNothingTicked: Story = {
  args: {
    live: true,
    draft: new Set(),
    statusLine: 'Revealed · Name & voice · to the table',
    effect: { kind: 'stop', label: 'Stop showing', notices: [] },
  },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getAllByRole('button', { name: 'Stop showing' })).toHaveLength(1)
  },
}

export const PlayersChosen: Story = {
  args: {
    audience: { kind: 'participants', ids: [BRANN.participant_id, ANA.participant_id] },
    draft: new Set(),
    statusLine: 'Choices reset for Brann and Ana',
    liveMessage: 'Choices reset for Brann and Ana',
    effect: { kind: 'none', label: 'Reveal', notices: [] },
  },
}

export const PlayersNoneChosen: Story = {
  args: {
    audience: { kind: 'participants', ids: [] },
    draft: new Set(),
    effect: { kind: 'none', label: 'Reveal', notices: ['Choose at least one player.'] },
  },
}

export const SeatsLoading: Story = { args: { seats: { status: 'loading', items: [] } } }
export const SeatsFailed: Story = { args: { seats: { status: 'failed', items: [] } } }
export const NoSeats: Story = { args: { seats: { status: 'ready', items: [] } } }

export const EffectReplace: Story = {
  args: {
    effect: { kind: 'replace', label: 'Reveal and replace', notices: ['This replaces what the table is seeing now.'] },
  },
}

export const EffectMove: Story = {
  args: {
    live: true,
    audience: { kind: 'participants', ids: [BRANN.participant_id] },
    effect: {
      kind: 'move',
      label: 'Move to Brann',
      notices: ['It stops showing to the table.', 'This replaces what Brann is seeing now.'],
    },
  },
}

export const PartialAudience: Story = {
  args: {
    live: true,
    partialAudience: true,
    audience: { kind: 'participants', ids: [BRANN.participant_id] },
    effect: { kind: 'move', label: 'Move to Brann', notices: [] },
  },
}

/** Every control disabled but Stop showing; the effect button is aria-disabled, not disabled. */
export const ConfirmInFlight: Story = {
  args: { confirming: true },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const revealing = canvas.getByRole('button', { name: 'Revealing…' })
    await expect(revealing).toHaveAttribute('aria-disabled', 'true')
    await expect(canvas.getByRole('button', { name: 'Stop showing' })).toBeEnabled()
    await expect(canvas.getByRole('button', { name: 'Cancel' })).toBeDisabled()
  },
}

export const Conflict: Story = {
  args: { conflict: true, liveMessage: 'Reveal changed — check and confirm again' },
}

export const Throttled: Story = {
  args: { error: 'Too many changes at once. Try again in 12 seconds.' },
}

export const FailedTryAgain: Story = {
  args: { error: "Couldn't reveal — Try again", tryAgain: true },
}

export const WaitingForStop: Story = {
  args: { stopWaiting: true, effect: { kind: 'replace', label: 'Reveal and replace', notices: [] } },
}

export const WithAProseAndHostileText: Story = {
  args: {
    rows: revealRows(NPC, { ...DATA, notes: '<img src=x onerror=alert(1)>\nA second line, kept as written.' }),
    draft: new Set(['name', 'voice', 'notes']),
  },
  play: async ({ canvasElement }) => {
    await expect(canvasElement.querySelector('img')).toBeNull()
    await expect(within(canvasElement).getByText(/<img src=x onerror=alert\(1\)>/)).toBeVisible()
  },
}

/** Dark Tavern: the same markup, the same tokens. */
export const ReadyDark: Story = {
  globals: { theme: 'dark' },
  args: { live: true, statusLine: 'Revealed · Name & voice · to the table', draft: new Set(['name', 'voice', 'qualifier']) },
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('dialog', { name: 'Reveal Ondrey' })).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Stop showing' })).toBeVisible()
  },
}

export const ErrorDark: Story = {
  globals: { theme: 'dark' },
  args: { error: "Couldn't reveal — Try again", tryAgain: true, conflict: true, staleNote: true },
}

/** Below 768px it is a full-screen sheet with no horizontal scroll. */
export const Phone: Story = {
  ...atViewport('phone375'),
  args: { audience: { kind: 'participants', ids: [BRANN.participant_id] }, effect: { kind: 'none', label: 'Reveal', notices: [] } },
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const sheet = within(canvasElement).getByRole('dialog', { name: 'Reveal Ondrey' })
    const box = sheet.getBoundingClientRect()
    await expect(Math.round(box.width)).toBe(375)
    await expect(Math.round(box.height)).toBe(812)
    await expectNoPageOverflow()
  },
}

export const PhoneDark: Story = {
  ...atViewport('phone375', 'dark'),
  args: { live: true, statusLine: 'Revealed · Name & voice · to the table' },
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    await expectTheme('dark')
    await expect(within(canvasElement).getByRole('dialog', { name: 'Reveal Ondrey' })).toBeVisible()
    await expectNoPageOverflow()
  },
}

