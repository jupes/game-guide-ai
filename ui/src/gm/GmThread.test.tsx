/**
 * GmThread (1kg.3.4): the GM's turn as narration, its outcome in the assistant
 * lane beneath it, and a reading order that is the visual order.
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GmThread } from './GmThread'
import { turnsFromTimeline } from './gmTimeline'
import type { GmTurn } from './gmTimeline'
import { LANE_COPY } from './laneState'
import { toolById } from './registry'
import {
  EDIT_ENTRY,
  OPAQUE_ENTRY,
  SOURCED_ANSWER,
  chatEntry,
  toolEntry,
} from './threadFixtures'

function renderThread(turns: readonly GmTurn[]) {
  return render(<GmThread turns={turns} />)
}

function exchanges(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('.gm-thread__exchange')]
}

/** True when `a` comes before `b` in document order. */
function precedes(a: Node, b: Node): boolean {
  return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
}

describe('GmThread — three lanes', () => {
  it('renders the GM turn as narration and the answer in the assistant lane beneath it', () => {
    const { container } = renderThread(turnsFromTimeline([chatEntry()]))
    const [exchange] = exchanges(container)

    const narration = exchange.querySelector('.chat-message--dm')
    const lane = exchange.querySelector('.assistant-lane')
    expect(narration).not.toBeNull()
    expect(lane).not.toBeNull()
    expect(within(narration as HTMLElement).getByText('Give me a drowned guardian for the marsh.')).toBeInTheDocument()
    expect(within(narration as HTMLElement).getByText('You')).toBeInTheDocument()
    // The answer is the assistant's, in the lane — never a narration bubble.
    expect(within(lane as HTMLElement).getByText(LANE_COPY.author)).toBeInTheDocument()
    expect(within(lane as HTMLElement).getByText('drowned guardian').tagName).toBe('STRONG')
    expect(exchange.querySelector('.chat-message--player')).toBeNull()
  })

  it('keeps DOM order equal to reading order: turn, then its lane, exchange by exchange', () => {
    const turns = turnsFromTimeline([
      chatEntry({ entry_id: 'ent_1', prompt: 'First question' }),
      chatEntry({ entry_id: 'ent_2', prompt: 'Second question' }),
    ])
    const { container } = renderThread(turns)
    const [first, second] = exchanges(container)

    const firstTurn = within(first).getByText('First question')
    const firstLane = first.querySelector('.assistant-lane') as HTMLElement
    const secondTurn = within(second).getByText('Second question')
    expect(precedes(firstTurn, firstLane)).toBe(true)
    expect(precedes(firstLane, secondTurn)).toBe(true)
  })

  it('renders a structured card at compact density inside the lane', () => {
    const { container } = renderThread(turnsFromTimeline([chatEntry()]))
    const lane = container.querySelector('.assistant-lane') as HTMLElement
    const card = within(lane).getByText('Tidewarden Drowned').closest('.game-content-card')
    expect(card).not.toBeNull()
    // Compact vitals, not the full stat-block sections.
    expect(within(lane).getByText('AC 16')).toBeInTheDocument()
    expect(within(lane).queryByText('Undertow')).toBeNull()
  })

  it('keeps the creative disclaimer visible for an invented answer', () => {
    renderThread(turnsFromTimeline([chatEntry()]))
    expect(screen.getByText(/Creative — may include invented content/)).toBeVisible()
  })

  it('makes no claim either way when groundedness was never recorded', () => {
    renderThread(turnsFromTimeline([chatEntry({ answer: { text: 'An old answer.', answerable: null, sources: null } })]))
    expect(screen.getByText('An old answer.')).toBeInTheDocument()
    expect(screen.queryByText(/Creative —/)).toBeNull()
    expect(screen.queryByText(/source/)).toBeNull()
  })

  it('renders citations compactly inside the lane, after the prose', () => {
    const { container } = renderThread(turnsFromTimeline([chatEntry({ answer: SOURCED_ANSWER })]))
    const lane = container.querySelector('.assistant-lane') as HTMLElement
    const sources = lane.querySelector('.gm-thread__sources .sources') as HTMLElement
    expect(sources).not.toBeNull()
    expect(within(sources).getByText('1 source')).toBeInTheDocument()
    expect(precedes(within(lane).getByText(/A basilisk petrifies/), sources)).toBe(true)
  })

  it('shows a turn with no stored answer as the GM’s turn alone', () => {
    const { container } = renderThread(turnsFromTimeline([chatEntry({ answer: null })]))
    expect(screen.getByText('Give me a drowned guardian for the marsh.')).toBeInTheDocument()
    expect(container.querySelector('.assistant-lane')).toBeNull()
  })
})

describe('GmThread — tool turns (RAIL-9)', () => {
  it('shows the brief as the GM’s turn with the tool’s label as a badge', () => {
    const { container } = renderThread(turnsFromTimeline([toolEntry()]))
    const narration = container.querySelector('.chat-message--dm') as HTMLElement
    expect(within(narration).getByText('Monster')).toHaveClass('gm-thread__tool')
    expect(narration).toHaveTextContent('CR 5, drowned')
  })

  it('shows the tool’s blurb in italics when an optional brief was left empty', () => {
    const recap = toolById('recap')
    const { container } = renderThread(turnsFromTimeline([
      toolEntry({ brief: '' }, { tool_id: 'recap' }),
    ]))
    const narration = container.querySelector('.chat-message--dm') as HTMLElement
    expect(within(narration).getByText(recap?.blurb ?? '').tagName).toBe('EM')
  })

  it('mounts the assistant lane hydrated, so a stored working run is checked on, never re-run (RAIL-21)', () => {
    renderThread(turnsFromTimeline([toolEntry()]))
    expect(screen.getByText('Checking on Monster…')).toBeInTheDocument()
  })

  it('gives an unknown tool no badge and a neutral lane (X-8)', () => {
    const [known] = turnsFromTimeline([toolEntry({ brief: 'something new' })])
    if (known.kind !== 'tool') throw new Error('expected a tool turn')
    // The wire refuses an id it does not know, so only a later bundle's
    // registry drift can put one here; stand for that with a cast.
    const unknown = { ...known, invocation: { ...known.invocation, tool_id: 'teleport' } } as unknown as GmTurn
    const { container } = renderThread([unknown])
    expect(container.querySelector('.gm-thread__tool')).toBeNull()
    expect(screen.getByText('something new')).toBeInTheDocument()
    expect(screen.getByText(LANE_COPY.newerVersion)).toBeInTheDocument()
  })
})

describe('GmThread — entries it cannot draw here', () => {
  it('shows RAIL-24’s placeholder for an entry this client cannot read', () => {
    renderThread(turnsFromTimeline([OPAQUE_ENTRY]))
    expect(screen.getByText(LANE_COPY.newerVersion)).toBeInTheDocument()
  })

  it('says an AI edit cannot be shown here yet, rather than guessing', () => {
    renderThread(turnsFromTimeline([EDIT_ENTRY]))
    expect(screen.getByText(LANE_COPY.unsupportedResult)).toBeInTheDocument()
  })
})

describe('GmThread — a turn in flight', () => {
  it('shows the lane working, with decorative dots and no live region of its own', () => {
    const { container } = renderThread([
      { kind: 'chat', key: 'live:1', prompt: 'Who runs the inn?', answer: { state: 'pending' } },
    ])
    const lane = container.querySelector('.assistant-lane') as HTMLElement
    expect(within(lane).getByText('Consulting the tomes…')).toBeInTheDocument()
    expect(lane.querySelector('.assistant-lane__dots')).toHaveAttribute('aria-hidden', 'true')
    // The pane announces the start and the arrival on its one live region
    // (ekf); a second live region here would double the start.
    expect(within(lane).queryByRole('status')).toBeNull()
  })

  it('shows a failed turn’s message in the lane', () => {
    const { container } = renderThread([
      { kind: 'chat', key: 'live:1', prompt: 'Who runs the inn?', answer: { state: 'failed', message: 'The service is busy.' } },
    ])
    const lane = container.querySelector('.assistant-lane') as HTMLElement
    expect(lane).toHaveAttribute('data-state', 'error')
    expect(within(lane).getByText('The service is busy.')).toBeInTheDocument()
  })
})

describe('GmThread — Load earlier (1kg.3.6)', () => {
  const turns = turnsFromTimeline([chatEntry()])

  it('draws no control when the thread fits in one page', () => {
    render(<GmThread turns={turns} />)
    expect(screen.queryByRole('button', { name: 'Load earlier' })).toBeNull()
  })

  it('offers Load earlier once the server has an older page, before the thread’s own turns', () => {
    render(<GmThread turns={turns} hasEarlier onLoadEarlier={vi.fn()} />)
    const button = screen.getByRole('button', { name: 'Load earlier' })
    const exchange = document.querySelector('.gm-thread__exchange') as HTMLElement
    expect(precedes(button, exchange)).toBe(true)
  })

  it('calls onLoadEarlier when pressed', async () => {
    const onLoadEarlier = vi.fn()
    render(<GmThread turns={turns} hasEarlier onLoadEarlier={onLoadEarlier} />)
    await userEvent.click(screen.getByRole('button', { name: 'Load earlier' }))
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
  })

  it('disables the control and swaps its label while a walk is in flight', () => {
    render(<GmThread turns={turns} hasEarlier loadingEarlier onLoadEarlier={vi.fn()} />)
    const button = screen.getByRole('button', { name: 'Loading…' })
    expect(button).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Load earlier' })).toBeNull()
  })

  it('shows a failed walk’s message beside the control without blanking the thread (STATE-1)', () => {
    render(
      <GmThread
        turns={turns}
        hasEarlier
        earlierError="Message history unavailable (503)."
        onLoadEarlier={vi.fn()}
      />,
    )
    expect(screen.getByText('Message history unavailable (503).')).toBeInTheDocument()
    // Retries in place (§12.2): the same control, not a separate Retry button.
    expect(screen.getByRole('button', { name: 'Load earlier' })).toBeInTheDocument()
    expect(screen.getByText('Give me a drowned guardian for the marsh.')).toBeInTheDocument()
  })

  it('carries no live region of its own — the pane’s single announcer speaks for it (ekf/4oz)', () => {
    render(
      <GmThread
        turns={turns}
        hasEarlier
        loadingEarlier
        earlierError="Message history unavailable (503)."
        onLoadEarlier={vi.fn()}
      />,
    )
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('draws nothing when hasEarlier is true but no handler is wired (defensive)', () => {
    render(<GmThread turns={turns} hasEarlier />)
    expect(screen.queryByRole('button', { name: /Load earlier|Loading/ })).toBeNull()
  })
})
