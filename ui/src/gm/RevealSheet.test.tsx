/**
 * RevealSheet.test.tsx -- the props-driven sheet (agent-forge-harness-1kg.7.3, brief 6,
 * tests 19, 22, 23, 26 to 28, and the Critic's items 12 and 13). The host decides what
 * the sheet shows; these tests prove what it renders and does for each state.
 */

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { documentTypeById, type DocumentType } from './registry'
import { RevealSheet, type RevealSheetProps } from './RevealSheet'
import { ANA, BRANN } from './revealFixtures'
import { revealRows, type RevealEffect } from './revealFields'

const NPC = documentTypeById('npc') as DocumentType
const NPC_DATA = {
  name: 'Ondrey',
  qualifier: 'Harbour almoner',
  voice: 'Quiet, clipped',
  wants: 'The family signet.',
  leverage: 'Her brother is in the hall.',
}

const REVEAL: RevealEffect = { kind: 'reveal', label: 'Reveal to the table', notices: [] }

function build(overrides: Partial<RevealSheetProps> = {}): RevealSheetProps {
  return {
    title: 'Ondrey',
    phase: 'ready',
    statusLine: "The table can't see this yet.",
    liveMessage: '',
    start: { pending: false, notice: null, retry: false },
    rows: revealRows(NPC, NPC_DATA),
    draft: new Set(['name', 'voice']),
    audience: { kind: 'table' },
    seats: { status: 'ready', items: [BRANN, ANA] },
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
    onStart: vi.fn(),
    onToggleRow: vi.fn(),
    onChooseTable: vi.fn(),
    onChoosePlayers: vi.fn(),
    onToggleSeat: vi.fn(),
    onRetrySeats: vi.fn(),
    onRetryPrepare: vi.fn(),
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    onStop: vi.fn(),
    restoreFocus: vi.fn(),
    ...overrides,
  }
}

const show = (overrides: Partial<RevealSheetProps> = {}) => {
  const props = build(overrides)
  return { props, ...render(<RevealSheet {...props} />) }
}

describe('the dialog (test 28, a11y)', () => {
  it('is a modal dialog named by its heading, and focus goes to the heading so the title is read first', () => {
    show()
    const dialog = screen.getByRole('dialog', { name: 'Reveal Ondrey' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const heading = within(dialog).getByRole('heading', { level: 2, name: 'Reveal Ondrey' })
    expect(heading).toHaveAttribute('tabindex', '-1')
    expect(heading).toHaveFocus()
  })

  it('Tab wraps from the last control to the first, and Shift+Tab from the heading to the last', async () => {
    const user = userEvent.setup()
    show({ live: true })
    const dialog = screen.getByRole('dialog')
    const stops = [...dialog.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled])')]
    const first = stops[0]
    const last = stops[stops.length - 1]
    last.focus()
    await user.tab()
    expect(first).toHaveFocus()
    screen.getByRole('heading', { level: 2 }).focus()
    await user.tab({ shift: true })
    expect(last).toHaveFocus()
  })

  it('returns focus where the host says when it goes away, once', () => {
    const { props, unmount } = show()
    expect(props.restoreFocus).not.toHaveBeenCalled()
    unmount()
    expect(props.restoreFocus).toHaveBeenCalledTimes(1)
  })

  it('mounts its status node empty, and fills the same node later', () => {
    const { rerender, props } = show()
    const node = screen.getByRole('status')
    expect(node).toHaveTextContent('')
    rerender(<RevealSheet {...props} liveMessage="Choices reset for Brann" />)
    expect(screen.getByRole('status')).toBe(node)
    expect(node).toHaveTextContent('Choices reset for Brann')
  })
})

describe('leaving (test 19, 23)', () => {
  it('Cancel, Escape and the scrim all cancel, and Confirm is never what they do', async () => {
    const user = userEvent.setup()
    const { props, container } = show()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.keyboard('{Escape}')
    fireEvent.mouseDown(container.querySelector('.gm-reveal__scrim') as HTMLElement)
    expect(props.onCancel).toHaveBeenCalledTimes(3)
    expect(props.onConfirm).not.toHaveBeenCalled()
  })

  it('a press inside the dialog does not reach the scrim', () => {
    const { props } = show()
    fireEvent.mouseDown(screen.getByRole('dialog'))
    expect(props.onCancel).not.toHaveBeenCalled()
  })

  it('Escape is always prevented, even while a Confirm is in flight, and then it cancels nothing (AE-50)', () => {
    const { props } = show({ confirming: true })
    const notPrevented = fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    // Mutation: allowing Escape in flight calls onCancel; not preventing it lets the shell's drawer act.
    expect(notPrevented).toBe(false)
    expect(props.onCancel).not.toHaveBeenCalled()
  })

  it('the scrim is inert while a Confirm is in flight', () => {
    const { props, container } = show({ confirming: true })
    fireEvent.mouseDown(container.querySelector('.gm-reveal__scrim') as HTMLElement)
    expect(props.onCancel).not.toHaveBeenCalled()
  })
})

describe('the audience: two native fieldsets, never checkboxes inside a radiogroup (Critic 12)', () => {
  it('Who sees it holds two radios and Players holds one checkbox per offered seat, labelled by alias', () => {
    show({ audience: { kind: 'participants', ids: [BRANN.participant_id] } })
    const who = screen.getByRole('group', { name: 'Who sees it' })
    expect(within(who).getAllByRole('radio').map((radio) => radio.parentElement?.textContent)).toEqual(['Whole table', 'Chosen players'])
    expect(screen.queryByRole('radiogroup')).toBeNull()
    const players = screen.getByRole('group', { name: 'Players' })
    expect(within(players).getByRole('checkbox', { name: 'Brann' })).toBeChecked()
    expect(within(players).getByRole('checkbox', { name: 'Ana' })).not.toBeChecked()
    expect(within(who).queryAllByRole('checkbox')).toEqual([])
  })

  it('the seats are disabled unless Chosen players is chosen, and the table radio reads checked', () => {
    show()
    expect(screen.getByRole('radio', { name: 'Whole table' })).toBeChecked()
    expect(screen.getByRole('group', { name: 'Players' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Brann' })).toBeDisabled()
  })

  it('choosing a radio and ticking a seat call the host', async () => {
    const user = userEvent.setup()
    const { props } = show({ audience: { kind: 'participants', ids: [] } })
    await user.click(screen.getByRole('radio', { name: 'Whole table' }))
    expect(props.onChooseTable).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('checkbox', { name: 'Brann' }))
    expect(props.onToggleSeat).toHaveBeenCalledWith(BRANN.participant_id, true)
  })

  it('Loading players… and Retry and No seated players yet. each leave Whole table usable', async () => {
    const user = userEvent.setup()
    const { props, rerender } = show({ seats: { status: 'loading', items: [] } })
    expect(screen.getByText('Loading players…')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Whole table' })).toBeEnabled()
    rerender(<RevealSheet {...props} seats={{ status: 'failed', items: [] }} />)
    expect(screen.getByText("Couldn't load players")).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(props.onRetrySeats).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('radio', { name: 'Whole table' })).toBeEnabled()
    rerender(<RevealSheet {...props} seats={{ status: 'ready', items: [] }} />)
    expect(screen.getByText('No seated players yet.')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Whole table' })).toBeEnabled()
  })
})

describe('the field rows', () => {
  it('a switch per row, named by the row, ticked by the draft, with the warning describing it', () => {
    show()
    expect(screen.getByRole('switch', { name: 'Name & voice' })).toBeChecked()
    expect(screen.getByRole('switch', { name: 'Qualifier' })).not.toBeChecked()
    const wants = screen.getByRole('switch', { name: 'Wants & leverage' })
    expect(wants).not.toBeChecked()
    expect(wants).toHaveAccessibleDescription('Would spoil the lie')
    expect(screen.getAllByText('Would spoil the lie')).toHaveLength(1)
  })

  it('offers no switch for a key off the allowlist (True identity, Tags)', () => {
    show()
    expect(screen.queryByRole('switch', { name: /true identity/i })).toBeNull()
    expect(screen.queryByRole('switch', { name: 'Tags' })).toBeNull()
  })

  it('a disabled row says why, in the switch description too', () => {
    show()
    const tell = screen.getByRole('switch', { name: 'Tell' })
    expect(tell).toBeDisabled()
    expect(tell).toHaveAccessibleDescription('Empty')
    const asset = {
      asset_id: 'ast_one', media_type: 'image', alt: 'A face', width: 10, height: 10,
    }
    const { unmount } = render(<RevealSheet {...build({ rows: revealRows(NPC, { ...NPC_DATA, portrait: asset }) })} />)
    expect(screen.getAllByRole('switch', { name: 'Portrait' }).at(-1)).toHaveAccessibleDescription("Pictures can't be shown to players yet")
    unmount()
  })

  it('a ticked row shows the exact text a player would see; an unticked one does not', () => {
    show()
    expect(screen.getByText('Quiet, clipped')).toBeInTheDocument()
    expect(screen.getByText('Ondrey')).toBeInTheDocument()
    expect(screen.queryByText('Harbour almoner')).toBeNull()
    expect(screen.queryByText('The family signet.')).toBeNull()
  })

  it('ticking a switch asks the host with the row and the next value', async () => {
    const user = userEvent.setup()
    const { props } = show()
    await user.click(screen.getByRole('switch', { name: 'Qualifier' }))
    expect(props.onToggleRow).toHaveBeenCalledWith(expect.objectContaining({ label: 'Qualifier' }), true)
  })

  it('renders field text as text nodes only: markup is shown, never parsed (test 27)', () => {
    const hostile = '<img src=x onerror=alert(1)> and <b>bold</b>'
    const { container } = show({
      rows: revealRows(NPC, { ...NPC_DATA, tell: hostile }),
      draft: new Set(['tell']),
    })
    // Mutation: innerHTML would create an <img> and a <b>.
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
    expect(screen.getByText(hostile)).toBeInTheDocument()
  })

  it('the empty document says so, offers no Confirm, and keeps the rows disabled', () => {
    show({
      rows: revealRows(NPC, { name: '' }),
      draft: new Set(),
      emptyDocument: true,
      effect: { kind: 'none', label: 'Reveal', notices: [] },
    })
    expect(screen.getByText('Nothing to reveal yet — this document is empty')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reveal' })).toBeDisabled()
    for (const toggle of screen.getAllByRole('switch')) expect(toggle).toBeDisabled()
  })
})

describe('the action (test 22)', () => {
  it('names its effect, and Confirm calls the host once', async () => {
    const user = userEvent.setup()
    const { props } = show()
    await user.click(screen.getByRole('button', { name: 'Reveal to the table' }))
    expect(props.onConfirm).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['reveal', 'Reveal to Brann'],
    ['replace', 'Reveal and replace'],
    ['update', 'Update'],
    ['move', 'Move to the table'],
  ] as const)('shows the %s label', (kind, label) => {
    show({ effect: { kind, label, notices: [] } })
    expect(screen.getByRole('button', { name: label })).toBeEnabled()
  })

  it('is disabled when nothing would change, and says so', () => {
    show({ effect: { kind: 'none', label: 'Update', notices: ['Nothing has changed.'] }, live: true })
    expect(screen.getByRole('button', { name: 'Update' })).toBeDisabled()
    expect(screen.getByText('Nothing has changed.')).toBeInTheDocument()
  })

  it('a live document with nothing ticked has ONE Stop showing and no second button of that name (Critic 11)', () => {
    show({ effect: { kind: 'stop', label: 'Stop showing', notices: [] }, live: true, draft: new Set() })
    expect(screen.getAllByRole('button', { name: 'Stop showing' })).toHaveLength(1)
  })

  it('Stop showing is offered only when the document is live', () => {
    const { rerender, props } = show()
    expect(screen.queryByRole('button', { name: 'Stop showing' })).toBeNull()
    rerender(<RevealSheet {...props} live />)
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
  })

  it('Stop showing calls the host at once', async () => {
    const user = userEvent.setup()
    const { props } = show({ live: true })
    await user.click(screen.getByRole('button', { name: 'Stop showing' }))
    expect(props.onStop).toHaveBeenCalledTimes(1)
  })

  it('shows the effect notices, the conflict line, the stale note, the partial note and a Stop that is still waiting', () => {
    show({
      effect: { kind: 'move', label: 'Move to Brann', notices: ['It stops showing to the table.', 'This replaces what Brann is seeing now.'] },
      conflict: true,
      staleNote: true,
      partialAudience: true,
      stopWaiting: true,
    })
    for (const line of [
      'It stops showing to the table.',
      'This replaces what Brann is seeing now.',
      'Reveal changed — check and confirm again',
      'The table is seeing an earlier version. To show the latest text, stop showing and reveal it again.',
      "Some players who can see this can't be chosen here.",
      'Waiting for Stop showing to finish…',
    ]) {
      expect(screen.getByText(line)).toBeInTheDocument()
    }
    // REVEAL-22: a widening waits for the Stop, it is not queued.
    expect(screen.getByRole('button', { name: 'Move to Brann' })).toBeDisabled()
  })

  it("an error is an alert, and Try again replaces the effect's name", async () => {
    const user = userEvent.setup()
    const { props } = show({ error: "Couldn't reveal — Try again", tryAgain: true })
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't reveal — Try again")
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(props.onConfirm).toHaveBeenCalledTimes(1)
  })
})

describe('a Confirm in flight (test 23, AE-50, the Critic 13)', () => {
  it('says Revealing…, keeps focus on the button, and leaves Stop showing the only enabled control', async () => {
    const user = userEvent.setup()
    const { props } = show({ confirming: false })
    const button = screen.getByRole('button', { name: 'Reveal to the table' })
    button.focus()
    expect(button).toHaveFocus()
    props.onConfirm = vi.fn()
    // Mutation: a natively `disabled` button drops focus to <body> inside the modal.
    const inFlight = render(<RevealSheet {...build({ confirming: true })} />)
    const revealing = inFlight.getAllByRole('button', { name: 'Revealing…' }).at(-1) as HTMLElement
    expect(revealing).toHaveAttribute('aria-disabled', 'true')
    expect(revealing).not.toBeDisabled()
    revealing.focus()
    await user.click(revealing)
    expect(revealing).toHaveFocus()
    const scope = within(inFlight.container)
    expect(scope.getByRole('button', { name: 'Stop showing' })).toBeEnabled()
    expect(scope.getByRole('button', { name: 'Cancel' })).toBeDisabled()
    for (const control of [...scope.getAllByRole('switch'), ...scope.getAllByRole('radio'), ...scope.getAllByRole('checkbox')]) {
      expect(control).toBeDisabled()
    }
  })

  it('a click on the in-flight button sends nothing', async () => {
    const user = userEvent.setup()
    const { props } = show({ confirming: true })
    await user.click(screen.getByRole('button', { name: 'Revealing…' }))
    expect(props.onConfirm).not.toHaveBeenCalled()
  })

  it('Stop showing is there to press while a Confirm is in flight, even for a document not yet live', async () => {
    const user = userEvent.setup()
    const { props } = show({ confirming: true, live: false })
    await user.click(screen.getByRole('button', { name: 'Stop showing' }))
    expect(props.onStop).toHaveBeenCalledTimes(1)
  })
})

describe('every other state (brief 6.3)', () => {
  it('preparing shows skeleton rows and visible text, with Cancel enabled and no switches', () => {
    const { container } = show({ phase: 'preparing' })
    expect(screen.getByText('Getting the document ready…')).toBeVisible()
    expect(container.querySelectorAll('.gm-reveal__skeleton[aria-hidden="true"] .gm-reveal__bar')).toHaveLength(3)
    expect(screen.queryAllByRole('switch')).toEqual([])
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })

  it('no session offers Start session, once per press, and names itself Start a table session', async () => {
    const user = userEvent.setup()
    const { props } = show({ phase: 'no_session' })
    expect(screen.getByRole('dialog', { name: 'Start a table session' })).toBeInTheDocument()
    expect(screen.getByText('Revealing needs a live table session. Starting one shows players nothing.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    expect(props.onStart).toHaveBeenCalledTimes(1)
    // No link, no QR, no token (I-1).
    expect(screen.queryByText(/link|QR|token/i)).toBeNull()
  })

  it('no session: Starting… while pending, each outcome in words, and Retry when it can be retried', () => {
    const { rerender, props } = show({ phase: 'no_session', start: { pending: true, notice: null, retry: false } })
    expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled()
    rerender(<RevealSheet {...props} start={{ pending: false, notice: 'A session is live in another campaign. End it there first.', retry: false }} />)
    expect(screen.getByRole('alert')).toHaveTextContent('A session is live in another campaign. End it there first.')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    rerender(<RevealSheet {...props} start={{ pending: false, notice: "Couldn't start a session.", retry: true }} />)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
  })

  it('unknown says so and offers only Cancel and Stop showing', () => {
    show({ phase: 'unknown' })
    expect(screen.getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    expect(screen.queryAllByRole('switch')).toEqual([])
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: /Reveal to|Update|Move/ })).toBeNull()
  })

  it('a seal that failed says nothing was lost and offers Try again', async () => {
    const user = userEvent.setup()
    const { props } = show({ phase: 'seal_failed' })
    expect(screen.getByText("Aetheril can't reach its library right now. Nothing was lost.")).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(props.onRetryPrepare).toHaveBeenCalledTimes(1)
  })

  it('a document that cannot be shown, or is not available, offers only Cancel', () => {
    const { rerender, props } = show({ phase: 'refused' })
    expect(screen.getByText("This document can't be shown right now.")).toBeInTheDocument()
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Cancel'])
    rerender(<RevealSheet {...props} phase="unavailable" />)
    expect(screen.getByText("This document isn't available.")).toBeInTheDocument()
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Cancel'])
  })

  it('the status line shows what the host says, and is not a live region', () => {
    show({ statusLine: 'Choices reset for Brann' })
    const line = screen.getByText('Choices reset for Brann')
    expect(line.closest('[role="status"]')).toBeNull()
  })
})
