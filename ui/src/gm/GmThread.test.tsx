/**
 * GmThread (1kg.3.4): the GM's turn as narration, its outcome in the assistant
 * lane beneath it, and a reading order that is the visual order.
 */

import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import type { MockInstance } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { GmThread } from './GmThread'
import { DIVIDER_COPY, collapseSessionSpans, formatDividerTime, turnsFromTimeline } from './gmTimeline'
import type { GmTurn } from './gmTimeline'
import { LANE_COPY } from './laneState'
import { toolById } from './registry'
import {
  DIVIDER_ENTRY,
  EDIT_ENTRY,
  END_DIVIDER_ENTRY,
  OPAQUE_ENTRY,
  QUIET_SESSION,
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

  // agent-forge-harness-ffz (pr120 review L-3): a mode chip keeps the same
  // conversation, so a Sage or Rules entry can land in the GM thread too. The
  // creative notice is the GM's own wording for the GM's own improvisation —
  // Sage and Rules show no such notice for an unanswerable reply anywhere
  // else in the app (ChatPane.tsx) — so it must not be pinned on their entries.
  it('does not label a Sage entry\'s unanswerable reply as "Creative", unlike a GM one (review L-3)', () => {
    renderThread(
      turnsFromTimeline([
        chatEntry({
          mode: 'sage',
          prompt: 'What is the range of fireball?',
          answer: { text: 'The sources do not cover that.', answerable: false, sources: [] },
        }),
      ]),
    )
    expect(screen.getByText('The sources do not cover that.')).toBeInTheDocument()
    expect(screen.queryByText(/Creative —/)).toBeNull()
  })

  it('does not label a Rules entry\'s unanswerable reply as "Creative" either (review L-3)', () => {
    renderThread(
      turnsFromTimeline([
        chatEntry({
          mode: 'rules',
          prompt: 'Can a rogue sneak attack twice in one turn?',
          answer: { text: 'The sources do not cover that.', answerable: false, sources: [] },
        }),
      ]),
    )
    expect(screen.queryByText(/Creative —/)).toBeNull()
  })

  it('makes no claim either way when groundedness was never recorded', () => {
    renderThread(turnsFromTimeline([chatEntry({ answer: { text: 'An old answer.', answerable: null, sources: null } })]))
    expect(screen.getByText('An old answer.')).toBeInTheDocument()
    expect(screen.queryByText(/Creative —/)).toBeNull()
    expect(screen.queryByText(/source/)).toBeNull()
  })

  // agent-forge-harness-ffz (pr120 review L-4, surviving mutant M13): dice is
  // parsed out of the answer text only when the answer is grounded
  // (`answerable === true`) — an invented (creative) answer that happens to
  // contain dice-shaped text must not get a dice chip, because nothing
  // vouches for that number the way a real roll would be.
  it('never shows a dice roll under a creative (non-grounded) answer, even when its text carries dice notation (review L-4)', () => {
    const { container } = renderThread(
      turnsFromTimeline([
        chatEntry({
          answer: { text: 'You roll 1d20+5=18 to swim through the current.', answerable: false, sources: [] },
        }),
      ]),
    )
    expect(screen.getByText(/You roll/)).toBeInTheDocument()
    expect(container.querySelector('.gm-thread__dice')).toBeNull()
  })

  it('shows a dice roll under a grounded answer whose text carries dice notation (review L-4, positive case)', () => {
    const { container } = renderThread(
      turnsFromTimeline([
        chatEntry({
          answer: { text: 'The trap deals 1d20+5=18 damage.', answerable: true, sources: [] },
        }),
      ]),
    )
    expect(container.querySelector('.gm-thread__dice')).not.toBeNull()
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
      { kind: 'chat', key: 'live:1', prompt: 'Who runs the inn?', answer: { state: 'pending' }, mode: 'gm' },
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
      { kind: 'chat', key: 'live:1', prompt: 'Who runs the inn?', answer: { state: 'failed', message: 'The service is busy.' }, mode: 'gm' },
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

describe('GmThread — Load earlier hands keyboard focus on (1kg.3.7)', () => {
  const newer = turnsFromTimeline([chatEntry({ entry_id: 'ent_new', prompt: 'A newer question' })])
  const older = turnsFromTimeline([chatEntry({ entry_id: 'ent_old', prompt: 'An older question' })])

  /** Presses the control (from the keyboard unless told otherwise), then plays the walk's renders as ChatPane would. */
  async function pressAndSettle(
    hasEarlier: boolean,
    whileLoading: () => void = () => {},
    via: 'keyboard' | 'mouse' = 'keyboard',
  ) {
    const onLoadEarlier = vi.fn()
    const view = render(<GmThread turns={newer} hasEarlier onLoadEarlier={onLoadEarlier} />)
    const button = screen.getByRole('button', { name: 'Load earlier' })
    if (via === 'mouse') {
      await userEvent.click(button)
    } else {
      button.focus()
      await userEvent.keyboard('{Enter}')
    }
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
    view.rerender(<GmThread turns={newer} hasEarlier loadingEarlier onLoadEarlier={onLoadEarlier} />)
    whileLoading()
    view.rerender(<GmThread turns={[...older, ...newer]} hasEarlier={hasEarlier} onLoadEarlier={onLoadEarlier} />)
    return view
  }

  it('moves focus to the first older turn once the last page lands and the control goes, like VersionList', async () => {
    const { container } = await pressAndSettle(false)
    expect(screen.queryByRole('button', { name: 'Load earlier' })).toBeNull()
    const [first] = exchanges(container)
    expect(first).toHaveTextContent('An older question')
    expect(first).toHaveAttribute('tabindex', '-1')
    expect(document.activeElement).toBe(first)
  })

  it('puts focus back on the control when the browser dropped it while the control was disabled', async () => {
    // A browser may drop focus to <body> once the button is disabled. jsdom
    // never does (and ignores blur() on a disabled button), so the test drops
    // it there itself: focus a stand-in, then remove it.
    await pressAndSettle(true, () => {
      const standIn = document.createElement('input')
      document.body.append(standIn)
      standIn.focus()
      standIn.remove()
      expect(document.activeElement).toBe(document.body)
    })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Load earlier' }))
  })

  it('never takes focus from wherever the reader moved it while the walk ran', async () => {
    const elsewhere = document.createElement('input')
    document.body.append(elsewhere)
    await pressAndSettle(false, () => elsewhere.focus())
    expect(document.activeElement).toBe(elsewhere)
    elsewhere.remove()
  })

  describe('without moving the view the reader holds (PR #136 review H1)', () => {
    // In a real browser focus() scrolls its target into view, which would undo
    // ChatPane's scroll hold (1kg.3.6). jsdom never scrolls, so these read the
    // options each focus() call was made with; the real-Chromium half is the
    // ChatPane story LoadEarlierByMouseHoldsTheReadersPlace.
    let focusSpy: MockInstance<HTMLElement['focus']>
    beforeEach(() => {
      focusSpy = vi.spyOn(HTMLElement.prototype, 'focus')
    })
    afterEach(() => focusSpy.mockRestore())

    /** The options of the last focus() call made on `el`. */
    function lastFocusOptions(el: Element): FocusOptions | undefined {
      const index = focusSpy.mock.contexts.lastIndexOf(el)
      expect(index).toBeGreaterThanOrEqual(0)
      return focusSpy.mock.calls[index][0]
    }

    /** What Chromium does to a focused button once it is disabled (jsdom never does). */
    function dropFocusToBody() {
      const standIn = document.createElement('input')
      document.body.append(standIn)
      standIn.focus()
      standIn.remove()
      expect(document.activeElement).toBe(document.body)
    }

    it.each(['keyboard', 'mouse'] as const)('puts focus back on the control without scrolling to it (%s press)', async (via) => {
      await pressAndSettle(true, dropFocusToBody, via)
      const button = screen.getByRole('button', { name: 'Load earlier' })
      expect(document.activeElement).toBe(button)
      expect(lastFocusOptions(button)).toEqual({ preventScroll: true })
    })

    it('hands focus to the first older turn without scrolling to it after a mouse press', async () => {
      const { container } = await pressAndSettle(false, undefined, 'mouse')
      const [first] = exchanges(container)
      expect(first).toHaveTextContent('An older question')
      expect(document.activeElement).toBe(first)
      expect(lastFocusOptions(first)).toEqual({ preventScroll: true })
    })

    it('scrolls to the first older turn after a keyboard press, so its focus ring is on screen', async () => {
      const { container } = await pressAndSettle(false)
      const [first] = exchanges(container)
      expect(document.activeElement).toBe(first)
      expect(lastFocusOptions(first)?.preventScroll ?? false).toBe(false)
    })
  })
})

// ── Session dividers (1kg.3.5) ───────────────────────────────────────────────

/** A fixed zone and locale, so a divider's time reads the same on every machine. */
const UTC_TIME = new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })
const formatUtc = (iso: string): string => UTC_TIME.format(new Date(iso))

function dividers(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('.gm-thread__divider')]
}

/** What a screen reader reads: the text, without anything `aria-hidden`. */
function readAloud(el: HTMLElement): string {
  const copy = el.cloneNode(true) as HTMLElement
  copy.querySelectorAll('[aria-hidden="true"]').forEach((node) => node.remove())
  return (copy.textContent ?? '').replace(/\s+/g, ' ').trim()
}

describe('GmThread — session dividers (1kg.3.5)', () => {
  // A played session (start, a turn, end), then a quiet one collapsed to a span.
  const turns = collapseSessionSpans(turnsFromTimeline([DIVIDER_ENTRY, chatEntry(), END_DIVIDER_ENTRY, ...QUIET_SESSION]))

  it('labels each divider with its time', () => {
    const { container } = render(<GmThread turns={turns} formatTime={formatUtc} />)
    const [start, end, span] = dividers(container)
    const datetimes = (el: HTMLElement) => [...el.querySelectorAll('time')].map((time) => time.getAttribute('datetime'))

    expect(start).toHaveAttribute('data-boundary', 'start')
    expect(readAloud(start)).toBe(`Session started ${formatUtc('2026-09-16T19:00:00Z')}`)
    expect(datetimes(start)).toEqual(['2026-09-16T19:00:00Z'])

    expect(end).toHaveAttribute('data-boundary', 'end')
    expect(readAloud(end)).toBe(`Session ended ${formatUtc('2026-09-16T23:00:00Z')}`)
    expect(datetimes(end)).toEqual(['2026-09-16T23:00:00Z'])

    // A quiet session reads "… to …"; the dash is drawn, never spoken.
    expect(span).toHaveAttribute('data-boundary', 'span')
    expect(readAloud(span)).toBe(
      `Session played ${formatUtc('2026-09-23T19:00:00Z')} to ${formatUtc('2026-09-23T22:30:00Z')}`,
    )
    expect(datetimes(span)).toEqual(['2026-09-23T19:00:00Z', '2026-09-23T22:30:00Z'])
    expect(within(span).getByText('–')).toHaveAttribute('aria-hidden', 'true')
    expect(within(span).getByText('to')).toHaveClass('gm-thread__sr-only')
  })

  it('writes a time in the reader’s own locale by default, and one it cannot read as it came', () => {
    const iso = '2026-09-16T19:00:00Z'
    const local = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(iso))
    expect(formatDividerTime(iso)).toBe(local)
    expect(formatDividerTime('not-a-date')).toBe('not-a-date')
    const { container } = render(<GmThread turns={turnsFromTimeline([DIVIDER_ENTRY])} />)
    expect(container.querySelector('.gm-thread__divider time')).toHaveTextContent(local)
  })

  it('a divider is text, not a control or a live region', () => {
    const { container } = render(<GmThread turns={turns} formatTime={formatUtc} />)
    const all = dividers(container)
    expect(all).toHaveLength(3)
    for (const divider of all) {
      // Reachable by Load earlier's hand-off alone, never by Tab.
      expect(divider.tabIndex).toBe(-1)
      expect(divider.querySelector('a, button, input, [tabindex]')).toBeNull()
      // Not an exchange, and never inside one.
      expect(divider.closest('.gm-thread__exchange')).toBeNull()
      // No role at all: role="separator" would make the label presentational.
      expect(divider.hasAttribute('role')).toBe(false)
      expect(divider.querySelector('[role]')).toBeNull()
      // No live region: the pane has one announcer (A-29).
      expect(divider.hasAttribute('aria-live')).toBe(false)
      expect(divider.querySelector('[aria-live]')).toBeNull()
      // The rules are decoration.
      const rules = [...divider.querySelectorAll('.gm-thread__divider-rule')]
      expect(rules).toHaveLength(2)
      for (const rule of rules) expect(rule).toHaveAttribute('aria-hidden', 'true')
    }
    expect(screen.queryByRole('separator')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
    // Found as the text it is.
    expect(screen.getByText(DIVIDER_COPY.start, { exact: false }).tagName).toBe('P')
    expect(screen.getByText(DIVIDER_COPY.end, { exact: false }).tagName).toBe('P')
    expect(screen.getByText(DIVIDER_COPY.span, { exact: false }).tagName).toBe('P')
  })

  it('draws each divider in its place between the exchanges, in reading order', () => {
    const { container } = render(<GmThread turns={turns} formatTime={formatUtc} />)
    const [start, end, span] = dividers(container)
    const [exchange] = exchanges(container)
    expect(precedes(start, exchange)).toBe(true)
    expect(precedes(exchange, end)).toBe(true)
    expect(precedes(end, span)).toBe(true)
  })

  it('Load earlier hands keyboard focus to a divider that arrives first', async () => {
    const newer = turnsFromTimeline([chatEntry({ entry_id: 'ent_new', prompt: 'A newer question' })])
    const older = turnsFromTimeline([DIVIDER_ENTRY, chatEntry({ entry_id: 'ent_old', prompt: 'An older question' })])
    const onLoadEarlier = vi.fn()
    const view = render(<GmThread turns={newer} hasEarlier onLoadEarlier={onLoadEarlier} formatTime={formatUtc} />)
    screen.getByRole('button', { name: 'Load earlier' }).focus()
    await userEvent.keyboard('{Enter}')
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
    view.rerender(<GmThread turns={newer} hasEarlier loadingEarlier onLoadEarlier={onLoadEarlier} formatTime={formatUtc} />)
    view.rerender(<GmThread turns={[...older, ...newer]} hasEarlier={false} onLoadEarlier={onLoadEarlier} formatTime={formatUtc} />)
    const [first] = dividers(view.container)
    expect(first).toHaveAttribute('data-boundary', 'start')
    expect(document.activeElement).toBe(first)
  })
})

describe('GmThread — the divider’s styles (1kg.3.5)', () => {
  const HERE = dirname(fileURLToPath(import.meta.url))
  // Comments hold prose, not rules.
  const THREAD_CSS = readFileSync(join(HERE, 'GmThread.css'), 'utf-8').replace(/\/\*[\s\S]*?\*\//g, ' ')

  /** Every block whose selector names `name`, with its body. */
  function rulesNaming(name: string): { selector: string; body: string }[] {
    return [...THREAD_CSS.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .map((match) => ({ selector: match[1].replace(/\s+/g, ' ').trim(), body: match[2] }))
      .filter((rule) => rule.selector.includes(name))
  }

  function body(selector: string): string {
    const found = rulesNaming(selector).filter((rule) => rule.selector === selector)
    expect(found).toHaveLength(1)
    return found[0].body
  }

  it('never move: no animation and no transition on any divider rule', () => {
    const rules = [...rulesNaming('.gm-thread__divider'), ...rulesNaming('.gm-thread__sr-only')]
    expect(rules.map((rule) => rule.selector)).toEqual([
      '.gm-thread__divider',
      '.gm-thread__divider:focus-visible',
      '.gm-thread__divider-rule',
      '.gm-thread__divider-label',
      '.gm-thread__sr-only',
    ])
    for (const { selector, body: declarations } of rules) {
      expect({ selector, moves: /(^|[;\s])(animation|transition)[a-z-]*\s*:/.test(declarations) }).toEqual({
        selector,
        moves: false,
      })
    }
  })

  it('wrap on a phone rather than scroll: the row wraps and the label may break anywhere', () => {
    expect(body('.gm-thread__divider')).toMatch(/(^|;)\s*flex-wrap:\s*wrap\s*;/)
    const label = body('.gm-thread__divider-label')
    expect(label).toMatch(/(^|;)\s*min-width:\s*0\s*;/)
    expect(label).toMatch(/(^|;)\s*overflow-wrap:\s*anywhere\s*;/)
    expect(label).not.toMatch(/white-space\s*:\s*nowrap/)
    expect(label).not.toMatch(/(^|;)\s*width\s*:/)
  })

  it('read at AA contrast in both themes: the one text colour is the token ds/contrast.test.ts proves on the surface', () => {
    // Axe cannot decide contrast over the parchment ground in the stories, so
    // the colour is pinned here to `on-surface-variant`, whose 4.5:1 on
    // `surface` is asserted per theme by src/ds/contrast.test.ts.
    expect(body('.gm-thread__divider')).toMatch(/(^|;)\s*color:\s*var\(--md-sys-color-on-surface-variant\)\s*;/)
    for (const selector of ['.gm-thread__divider-label', '.gm-thread__divider-rule', '.gm-thread__sr-only']) {
      expect({ selector, paints: /(^|[;\s])(color|opacity)\s*:/.test(body(selector)) }).toEqual({ selector, paints: false })
    }
  })

  it('show where Load earlier’s hand-off landed, and draw the rules as borders', () => {
    expect(body('.gm-thread__divider:focus-visible')).toMatch(/outline:\s*3px solid var\(--md-sys-color-secondary\)/)
    expect(body('.gm-thread__divider-rule')).toMatch(/border-top:\s*1px solid var\(--md-sys-color-outline-variant\)/)
  })
})
