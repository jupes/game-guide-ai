/**
 * AssistantLane — §3.4's table, row by row (1kg.3.2).
 *
 * The lane is where a GM learns whether the thing they paid for is running,
 * finished, throttled or gone, so the assertions here are mostly about two
 * things the handoff component got wrong: what is on screen in each state, and
 * what a screen reader is told and how often.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { AssistantLane } from './AssistantLane'
import type { AssistantLaneProps } from './AssistantLane'
import { LANE_COPY, STILL_WORKING_MS } from './laneState'
import {
  ALL_ENABLED,
  DEFAULT_AVAILABILITY,
  SUGGESTIONS,
  cardResult,
  documentResult,
  errorInfo,
  manualTimer,
  mediaResult,
  toolInvocation,
} from './laneFixtures'

function handlers() {
  return {
    onOpenDocument: vi.fn(),
    onArmSuggestion: vi.fn(),
    onCancel: vi.fn(),
    onRetry: vi.fn(),
    onEditBrief: vi.fn(),
    onCheckAgain: vi.fn(),
    onRunAgain: vi.fn(),
  }
}

function renderLane(props: Partial<AssistantLaneProps> = {}) {
  const spies = handlers()
  const base: AssistantLaneProps = {
    invocation: toolInvocation(),
    availability: ALL_ENABLED,
    ...spies,
  }
  const view = render(<AssistantLane {...base} {...props} />)
  return {
    ...view,
    ...spies,
    /** Re-render the same lane with different props, keeping the spies. */
    update(next: Partial<AssistantLaneProps>) {
      view.rerender(<AssistantLane {...base} {...props} {...next} />)
    },
  }
}

const status = () => screen.getByRole('status')
const failed = (error: Record<string, unknown>) =>
  toolInvocation({ status: 'failed', error: errorInfo(error) })

describe('AssistantLane — working (RAIL-15)', () => {
  it('badges the tool from the registry and shows its working label', () => {
    renderLane()
    expect(screen.getByText('NPC')).toBeInTheDocument()
    expect(status()).toHaveTextContent('Writing the dossier…')
  })

  it('puts the working label in a POLITE live region, never an alert', () => {
    renderLane()
    expect(status()).toHaveAttribute('aria-live', 'polite')
  })

  it('offers Cancel, in the record words, and calls back once', async () => {
    const lane = renderLane()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(lane.onCancel).toHaveBeenCalledTimes(1)
    expect(lane.onRetry).not.toHaveBeenCalled()
  })

  it('renders NO result while working, even when one was there a moment ago', () => {
    // AC 1 end to end: the union has nowhere to keep the card, so a lane that
    // goes back to working cannot leave it under the header.
    const lane = renderLane({
      invocation: toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() }),
    })
    expect(screen.getByText('Drowned Thing')).toBeInTheDocument()

    lane.update({ invocation: toolInvocation({ tool_id: 'monster' }) })
    expect(screen.queryByText('Drowned Thing')).toBeNull()
    expect(status()).toHaveTextContent('Building the stat block…')
  })

  it('hides the three-dot indicator from assistive technology', () => {
    const { container } = renderLane()
    expect(container.querySelector('.assistant-lane__dots')).toHaveAttribute('aria-hidden', 'true')
  })

  it('reads `Cancelling…` once a cancel is in flight, and keeps Cancel reachable (RAIL-23)', () => {
    // The button stays enabled rather than disabled: a disabled control drops
    // focus to <body>, and a repeat cancel is idempotent anyway.
    renderLane({ invocation: toolInvocation({ cancel_requested: true }) })
    expect(status()).toHaveTextContent(LANE_COPY.cancelling)
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })
})

describe('AssistantLane — the 30 s clock (RAIL-15)', () => {
  it('schedules exactly one wait, for 30 s', () => {
    const timer = manualTimer()
    renderLane({ timer })
    expect(timer.pending).toEqual([STILL_WORKING_MS])
  })

  it('switches to `Still working…` when it fires, without any waiting', () => {
    const timer = manualTimer()
    renderLane({ timer })
    act(() => {
      timer.fire()
    })
    expect(status()).toHaveTextContent(LANE_COPY.stillWorking)
  })

  it('starts the clock again for a different run', () => {
    const timer = manualTimer()
    const lane = renderLane({ timer })
    act(() => {
      timer.fire()
    })
    expect(status()).toHaveTextContent(LANE_COPY.stillWorking)

    lane.update({ invocation: toolInvocation({ invocation_id: 'inv_0a1b2c3d4e5f6a7b' }) })
    expect(status()).toHaveTextContent('Writing the dossier…')
    expect(timer.pending).toEqual([STILL_WORKING_MS])
  })

  it('starts the clock again for a RETRY, which re-sends the same invocation id (RAIL-18)', () => {
    // The id is the idempotency key, so only the attempt tells two runs apart.
    const timer = manualTimer()
    const lane = renderLane({ timer })
    act(() => {
      timer.fire()
    })
    expect(status()).toHaveTextContent(LANE_COPY.stillWorking)

    lane.update({ invocation: toolInvocation({ attempt: 2 }) })
    expect(status()).toHaveTextContent('Writing the dossier…')
  })

  it('runs no clock for a lane that is not working', () => {
    const timer = manualTimer()
    renderLane({ timer, invocation: toolInvocation({ status: 'done', result: documentResult() }) })
    expect(timer.pending).toEqual([])
  })

  it('runs no clock for RAIL-21 — an unknown run is checked on, not timed', () => {
    const timer = manualTimer()
    renderLane({ timer, hydrated: true })
    expect(timer.pending).toEqual([])
  })

  it('cancels the clock when the run finishes', () => {
    const timer = manualTimer()
    const lane = renderLane({ timer })
    lane.update({ invocation: toolInvocation({ status: 'cancelled' }) })
    expect(timer.pending).toEqual([])
  })
})

describe('AssistantLane — unknown (RAIL-21)', () => {
  it('reads `Checking on <label>…` for a hydrated working entry', () => {
    renderLane({ hydrated: true })
    expect(status()).toHaveTextContent('Checking on NPC…')
  })

  it('offers Check again and Cancel, and nothing that re-runs the tool', async () => {
    const lane = renderLane({ lost: true })
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Run again' })).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Check again' }))
    expect(lane.onCheckAgain).toHaveBeenCalledTimes(1)
    expect(lane.onRetry).not.toHaveBeenCalled()
  })
})

describe('AssistantLane — done (RAIL-17, CANVAS-3)', () => {
  const doneDocument = toolInvocation({ status: 'done', result: documentResult() })

  it('renders prose through the Markdown component', () => {
    const { container } = renderLane({
      invocation: toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() }),
    })
    expect(container.querySelector('.assistant-text strong')?.textContent).toBe('grapple pressure')
  })

  it('renders a document result as the CANVAS-3 link row', () => {
    renderLane({ invocation: doneDocument })
    const row = screen.getByRole('button', { name: /Open in canvas/ })
    expect(within(row).getByText('NPC Dossier · saved to NPCs')).toBeInTheDocument()
  })

  it('opens nothing by itself — CANVAS-1 belongs to the surface, not the lane', () => {
    const lane = renderLane({ invocation: doneDocument })
    expect(lane.onOpenDocument).not.toHaveBeenCalled()
  })

  it('hands the document back when the row is activated', async () => {
    const lane = renderLane({ invocation: doneDocument })
    await userEvent.click(screen.getByRole('button', { name: /Open in canvas/ }))
    expect(lane.onOpenDocument).toHaveBeenCalledTimes(1)
  })

  it('composes a card result as a COMPACT card — the lane supplies the chrome', () => {
    const { container } = renderLane({
      invocation: toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() }),
    })
    expect(screen.getByText('Drowned Thing')).toBeInTheDocument()
    expect(container.querySelector('.stat-block-card__abilities--compact')).not.toBeNull()
  })

  it('announces `<label> finished`, and does not show it', () => {
    const { container } = renderLane({ invocation: doneDocument })
    expect(status()).toHaveTextContent('NPC finished')
    expect(container.querySelector('.assistant-lane__sr-only')?.textContent).toBe('NPC finished')
  })

  it('keeps a result that beat its own cancel, and says so both ways (RAIL-23)', () => {
    renderLane({
      invocation: toolInvocation({ status: 'done', cancel_requested: true, result: documentResult() }),
    })
    expect(screen.getByRole('button', { name: /Open in canvas/ })).toBeInTheDocument()
    expect(screen.getByText(LANE_COPY.lateFinish)).toBeInTheDocument()
    expect(status()).toHaveTextContent(LANE_COPY.lateFinish)
  })

  it('renders no prose block when the result carries none', () => {
    const { container } = renderLane({
      invocation: toolInvocation({ status: 'done', result: documentResult({ prose: '' }) }),
    })
    expect(container.querySelector('.assistant-text')).toBeNull()
  })

  it('never loads a remote image out of assistant prose (X-10, AE-66)', () => {
    const { container } = renderLane({
      invocation: toolInvocation({
        status: 'done',
        result: documentResult({ prose: 'look ![x](https://example.test/p.png) here' }),
      }),
    })
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('AssistantLane — a result kind this bead does not own (RAIL-24, X-8)', () => {
  it('stands in with a neutral placeholder, never a borrowed card', () => {
    renderLane({
      invocation: toolInvocation({ tool_id: 'portrait', status: 'done', result: mediaResult() }),
    })
    expect(screen.getByText(LANE_COPY.unsupportedResult)).toBeInTheDocument()
    expect(screen.getByText('Portrait')).toBeInTheDocument()
    expect(screen.queryByText('NPC')).toBeNull()
  })

  it('still renders the prose and the badge around the placeholder', () => {
    const { container } = renderLane({
      invocation: toolInvocation({ tool_id: 'portrait', status: 'done', result: mediaResult() }),
    })
    expect(container.querySelector('.assistant-text')?.textContent).toContain('A study in lamplight')
  })
})

describe('AssistantLane — suggestions arm the composer (RAIL-8, X-1)', () => {
  const withSuggestions = toolInvocation({
    status: 'done',
    result: documentResult({ suggestions: SUGGESTIONS }),
  })

  it('renders every validated suggestion, at most three', () => {
    renderLane({ invocation: withSuggestions })
    for (const suggestion of SUGGESTIONS) {
      expect(screen.getByRole('button', { name: suggestion.label })).toBeInTheDocument()
    }
  })

  it('ARMS: it hands back a command, a brief and a source, and runs nothing', async () => {
    const lane = renderLane({ invocation: withSuggestions, sourceEntryId: 'ent_1' })
    await userEvent.click(screen.getByRole('button', { name: 'Build an encounter around it' }))
    expect(lane.onArmSuggestion).toHaveBeenCalledExactlyOnceWith({
      toolId: 'encounter',
      command: '/encounter',
      brief: 'an ambush at the crossing',
      sourceEntryId: 'ent_1',
    })
    expect(lane.onRetry).not.toHaveBeenCalled()
    expect(lane.onOpenDocument).not.toHaveBeenCalled()
  })

  it('drops a suggestion whose target is capability-disabled (AE-12 next door)', () => {
    const disabled = toolInvocation({
      status: 'done',
      result: documentResult({
        suggestions: [{ tool_id: 'portrait', label: 'Portrait', icon: 'image', brief: null }],
      }),
    })
    renderLane({ invocation: disabled, availability: DEFAULT_AVAILABILITY })
    expect(screen.queryByRole('button', { name: 'Portrait' })).toBeNull()
  })

  it('offers nothing before the capability lookup answers (AE-58)', () => {
    renderLane({ invocation: withSuggestions, availability: undefined })
    expect(screen.queryByRole('button', { name: 'Three hooks' })).toBeNull()
  })

  it('never writes a brief into an attribute (X-7)', () => {
    const { container } = renderLane({ invocation: withSuggestions, sourceEntryId: 'ent_1' })
    for (const element of container.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        expect(attribute.value).not.toContain('an ambush at the crossing')
      }
    }
  })
})

describe('AssistantLane — failures (RAIL-18 to RAIL-20)', () => {
  it('shows the server message and Try again for a retryable failure', async () => {
    const lane = renderLane({ invocation: failed({ retryable: true }) })
    expect(status()).toHaveTextContent("Aetheril can't reach its library right now.")
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(lane.onRetry).toHaveBeenCalledTimes(1)
  })

  it('offers Edit brief, not Try again, for a final failure', async () => {
    const lane = renderLane({
      invocation: failed({ code: 'brief_too_long', message: 'That brief is too long.', retryable: false }),
    })
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Edit brief' }))
    expect(lane.onEditBrief).toHaveBeenCalledTimes(1)
  })

  it('tells the per-user window when to come back', () => {
    renderLane({
      invocation: failed({ code: 'throttled_user', message: 'Too many.', retryable: true, retry_after_s: 45 }),
    })
    expect(status()).toHaveTextContent("That's a lot at once — try again in 45 seconds")
  })

  it('offers NO action at all for the pilot daily cap', () => {
    renderLane({ invocation: failed({ code: 'throttled_daily', message: 'Spent.', retryable: false }) })
    expect(status()).toHaveTextContent(LANE_COPY.dailyCap)
    expect(screen.queryAllByRole('button')).toEqual([])
  })

  it('renders NO result while in error, even when one was there a moment ago', () => {
    const lane = renderLane({
      invocation: toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() }),
    })
    expect(screen.getByText('Drowned Thing')).toBeInTheDocument()

    lane.update({ invocation: toolInvocation({ tool_id: 'monster', status: 'failed', error: errorInfo() }) })
    expect(screen.queryByText('Drowned Thing')).toBeNull()
  })

  it('hides the error icon from assistive technology and leaves the text to the region', () => {
    const { container } = renderLane({ invocation: failed({}) })
    expect(container.querySelector('.assistant-lane__error-icon')).toHaveAttribute('aria-hidden', 'true')
  })
})

describe('AssistantLane — cancelled (RAIL-22)', () => {
  it('reads `Cancelled.` and offers Run again', async () => {
    const lane = renderLane({ invocation: toolInvocation({ status: 'cancelled' }) })
    expect(status()).toHaveTextContent(LANE_COPY.cancelled)
    await userEvent.click(screen.getByRole('button', { name: 'Run again' }))
    expect(lane.onRunAgain).toHaveBeenCalledTimes(1)
  })
})

describe('AssistantLane — unknown is not NPC (RAIL-24, X-8)', () => {
  it('renders the neutral placeholder and no badge for an unreadable entry', () => {
    const { container } = renderLane({ invocation: null })
    expect(screen.getByText(LANE_COPY.newerVersion)).toBeInTheDocument()
    expect(container.querySelector('.assistant-lane__tool')).toBeNull()
    expect(screen.queryAllByRole('button')).toEqual([])
  })
})

describe('AssistantLane — announcements (STATE-7)', () => {
  it('keeps exactly one live region, whatever the state', () => {
    const lane = renderLane()
    expect(screen.getAllByRole('status')).toHaveLength(1)
    lane.update({ invocation: toolInvocation({ status: 'done', result: documentResult() }) })
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('does not re-announce on a re-render that changes nothing', () => {
    // The region is never remounted and its text is derived, so an unrelated
    // re-render mutates nothing and a screen reader stays quiet.
    const lane = renderLane({ invocation: toolInvocation({ status: 'done', result: documentResult() }) })
    const region = status()
    const before = region.textContent
    lane.update({})
    expect(status()).toBe(region)
    expect(status().textContent).toBe(before)
  })

  it('announces the finish when the state actually changes', () => {
    const lane = renderLane()
    expect(status()).toHaveTextContent('Writing the dossier…')
    lane.update({ invocation: toolInvocation({ status: 'done', result: documentResult() }) })
    expect(status()).toHaveTextContent('NPC finished')
  })
})

describe('AssistantLane — focus', () => {
  it('leaves focus where it was: nothing in the lane steals it (CANVAS-1, AE-15)', () => {
    render(<button type="button">composer</button>)
    const composer = screen.getByRole('button', { name: 'composer' })
    composer.focus()

    const lane = renderLane()
    lane.update({ invocation: toolInvocation({ status: 'done', result: documentResult() }) })
    expect(document.activeElement).toBe(composer)
  })

  it('catches focus at the lane heading when its control ceases to exist (LAYOUT-6)', async () => {
    const lane = renderLane()
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    await userEvent.click(cancel)
    expect(document.activeElement).toBe(cancel)

    lane.update({ invocation: toolInvocation({ status: 'done', result: documentResult() }) })
    expect(document.activeElement).toHaveClass('assistant-lane__header')
    expect(document.activeElement).not.toBe(document.body)
  })

  it('does not grab focus back once the GM has moved it out of the lane', async () => {
    // The GM presses Cancel, then clicks into the composer. The run finishing
    // must not pull them out of it again (CANVAS-1).
    render(<button type="button">composer</button>)
    const composer = screen.getByRole('button', { name: 'composer' })

    const lane = renderLane()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    composer.focus()

    lane.update({ invocation: toolInvocation({ status: 'done', result: documentResult() }) })
    expect(document.activeElement).toBe(composer)
  })

  it('keeps the heading out of the tab order', () => {
    const { container } = renderLane()
    expect(container.querySelector('.assistant-lane__header')).toHaveAttribute('tabindex', '-1')
  })
})

describe('AssistantLane — actions are only offered when they can work', () => {
  it('renders no action when the surface passed no handler for it', () => {
    render(<AssistantLane invocation={toolInvocation()} onOpenDocument={vi.fn()} />)
    expect(screen.queryAllByRole('button')).toEqual([])
  })
})

describe('AssistantLane — prefers-reduced-motion', () => {
  let original: typeof window.matchMedia

  beforeEach(() => {
    original = window.matchMedia
  })

  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', { writable: true, value: original })
  })

  function stub(reduce: boolean) {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: (query: string) => ({
        matches: query === '(prefers-reduced-motion: reduce)' ? reduce : !reduce,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    })
  }

  it('does NOT animate the three dots when reduced motion is asked for', () => {
    stub(true)
    const { container } = renderLane()
    expect(container.querySelector('.assistant-lane__dots')).not.toHaveClass('assistant-lane__dots--animated')
  })

  it('animates them when it is not', () => {
    stub(false)
    const { container } = renderLane()
    expect(container.querySelector('.assistant-lane__dots')).toHaveClass('assistant-lane__dots--animated')
  })
})

// ── Tokens, not hex (both themes follow the palette) ─────────────────────────

describe('the lane stylesheets', () => {
  const here = dirname(fileURLToPath(import.meta.url))
  const sheets = ['AssistantLane.css', 'AssistantText.css', 'AssistantDocumentLink.css']

  it.each(sheets)('%s names no colour of its own', (sheet) => {
    // A hard-coded colour is invisible in light mode and wrong in dark; the
    // theme tests cannot catch it, so it is caught here instead.
    const css = readFileSync(join(here, sheet), 'utf8')
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(css).not.toMatch(/\b(rgb|rgba|hsl|hsla)\(/)
  })

  it('guards the dot animation behind prefers-reduced-motion', () => {
    const css = readFileSync(join(here, 'AssistantLane.css'), 'utf8')
    const guard = css.indexOf('@media (prefers-reduced-motion: no-preference)')
    expect(guard).toBeGreaterThan(-1)
    expect(css.indexOf('animation:')).toBeGreaterThan(guard)
  })
})
