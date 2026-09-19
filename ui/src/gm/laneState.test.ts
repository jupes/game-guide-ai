/**
 * laneState — the §3.4 table as tests (1kg.3.2).
 *
 * The view is where "no children while working or in error" is decided, so most
 * of these assertions are about which *shape* comes back rather than about
 * copy: a `working` view has no `result` key to hold a stale card, and that is
 * checked here rather than inferred from the JSX.
 */

import { describe, expect, it } from 'vitest'
import type { ToolInvocation, ToolSuggestion } from './contracts'
import {
  LANE_COPY,
  describeRetryWindow,
  documentLinkMeta,
  laneChips,
  normaliseLane,
  realTimer,
} from './laneState'
import {
  ALL_ENABLED,
  DEFAULT_AVAILABILITY,
  DOCUMENT_LINK,
  SUGGESTIONS,
  cardResult,
  documentResult,
  errorInfo,
  mediaResult,
  toolInvocation,
} from './laneFixtures'

describe('normaliseLane — working (RAIL-15)', () => {
  it('shows the tool working label and offers Cancel', () => {
    const view = normaliseLane(toolInvocation())
    expect(view.state).toBe('working')
    if (view.state !== 'working') throw new Error('unreachable')
    expect(view.status).toBe('Writing the dossier…')
    expect(view.actions).toEqual(['cancel'])
    expect(view.cancelling).toBe(false)
  })

  it('carries no result and no suggestions — a stale child has nowhere to go', () => {
    // AC 1, by construction. If someone widens the union, this stops compiling
    // before it stops passing.
    const view = normaliseLane(toolInvocation())
    expect(Object.keys(view)).not.toContain('result')
    expect(Object.keys(view)).not.toContain('chips')
  })

  it('becomes `Still working…` once the 30 s timer has fired', () => {
    const view = normaliseLane(toolInvocation(), { stillWorking: true })
    expect(view.state === 'working' && view.status).toBe(LANE_COPY.stillWorking)
  })

  it('reads `Cancelling…` while a cancel is in flight (RAIL-23)', () => {
    const view = normaliseLane(toolInvocation({ cancel_requested: true }))
    expect(view.state === 'working' && view.status).toBe(LANE_COPY.cancelling)
    expect(view.state === 'working' && view.cancelling).toBe(true)
  })

  it('lets `Cancelling…` win over `Still working…` — the newer fact is the useful one', () => {
    const view = normaliseLane(toolInvocation({ cancel_requested: true }), { stillWorking: true })
    expect(view.state === 'working' && view.status).toBe(LANE_COPY.cancelling)
  })
})

describe('normaliseLane — unknown (RAIL-21)', () => {
  it('maps a hydrated `working` entry to Checking, never back to working', () => {
    const view = normaliseLane(toolInvocation(), { hydrated: true })
    expect(view.state).toBe('checking')
    expect(view.state === 'checking' && view.status).toBe('Checking on NPC…')
  })

  it('maps a lost response to Checking', () => {
    expect(normaliseLane(toolInvocation(), { lost: true }).state).toBe('checking')
  })

  it('offers Check again and Cancel, and nothing that would re-run it', () => {
    const view = normaliseLane(toolInvocation(), { lost: true })
    expect(view.state === 'checking' && view.actions).toEqual(['check-again', 'cancel'])
  })

  it('leaves a terminal status alone even when the entry was hydrated', () => {
    // A stored `done` is a result, not a mystery; only `working` is unknown.
    const done = toolInvocation({ status: 'done', result: documentResult() })
    expect(normaliseLane(done, { hydrated: true }).state).toBe('done')
  })
})

describe('normaliseLane — done (RAIL-17, RAIL-23)', () => {
  const done = toolInvocation({ status: 'done', result: documentResult() })

  it('announces `<label> finished` exactly once, in the record words', () => {
    const view = normaliseLane(done)
    expect(view.state === 'done' && view.announcement).toBe('NPC finished')
  })

  it('keeps a result that finished before its cancel landed, and says so', () => {
    const late = toolInvocation({ status: 'done', cancel_requested: true, result: documentResult() })
    const view = normaliseLane(late)
    if (view.state !== 'done') throw new Error('unreachable')
    expect(view.note).toBe(LANE_COPY.lateFinish)
    expect(view.announcement).toContain(LANE_COPY.lateFinish)
    expect(view.result.result_kind).toBe('document')
  })

  it('offers no state actions: done is not a retry surface', () => {
    expect(Object.keys(normaliseLane(done))).not.toContain('actions')
  })

  it('falls back to the neutral placeholder when a `done` carries no result', () => {
    // The schema forbids it; a server that breaks the promise still must not
    // render a blank lane.
    const broken = { ...done, result: null } as ToolInvocation
    expect(normaliseLane(broken)).toEqual({ state: 'unreadable', placeholder: LANE_COPY.newerVersion })
  })
})

describe('normaliseLane — failures (RAIL-18 to RAIL-20)', () => {
  function failed(error: Record<string, unknown>) {
    return normaliseLane(toolInvocation({ status: 'failed', error: errorInfo(error) }))
  }

  it('offers Try again for a retryable failure, with the server message', () => {
    const view = failed({ code: 'backend_unavailable', retryable: true })
    if (view.state !== 'error') throw new Error('unreachable')
    expect(view.message).toBe("Aetheril can't reach its library right now. Nothing was lost.")
    expect(view.actions).toEqual(['try-again'])
  })

  it('offers Try again for RAIL-27 — a stuck attempt the server expired', () => {
    const view = failed({ code: 'attempt_expired', message: 'That attempt stopped responding.', retryable: true })
    expect(view.state === 'error' && view.actions).toEqual(['try-again'])
  })

  it('offers Edit brief for a final failure (RAIL-19)', () => {
    const view = failed({ code: 'brief_too_long', message: 'That brief is too long.', retryable: false })
    expect(view.state === 'error' && view.actions).toEqual(['edit-brief'])
  })

  it('tells the per-user window when to come back (RAIL-20)', () => {
    const view = failed({ code: 'throttled_user', message: 'Too many.', retryable: true, retry_after_s: 90 })
    if (view.state !== 'error') throw new Error('unreachable')
    expect(view.message).toBe("That's a lot at once — try again in 2 minutes")
    expect(view.actions).toEqual(['try-again'])
  })

  it('offers NO retry for the pilot daily cap, and says when it comes back', () => {
    const view = failed({ code: 'throttled_daily', message: 'Spent.', retryable: false })
    if (view.state !== 'error') throw new Error('unreachable')
    expect(view.message).toBe(LANE_COPY.dailyCap)
    expect(view.actions).toEqual([])
  })

  it('treats an unmarked, retryable 429 as RAIL-18 rather than inventing a window', () => {
    const view = failed({ code: 'unknown_to_this_client', message: 'Slow down.', retryable: true })
    expect(view.state === 'error' && view.actions).toEqual(['try-again'])
  })

  it('carries no children — an error never shows the last result (RAIL-18, STATE-1)', () => {
    expect(Object.keys(failed({}))).not.toContain('result')
  })

  it('falls back to the placeholder when a `failed` carries no error body', () => {
    const broken = { ...toolInvocation({ status: 'failed', error: errorInfo() }), error: null } as ToolInvocation
    expect(normaliseLane(broken).state).toBe('unreadable')
  })
})

describe('describeRetryWindow (RAIL-20)', () => {
  it.each([
    [0, 'now'],
    [1, 'in 1 second'],
    [45, 'in 45 seconds'],
    [60, 'in 1 minute'],
    [61, 'in 2 minutes'],
    [3600, 'in 60 minutes'],
  ])('reads %i seconds as "%s"', (seconds, expected) => {
    expect(describeRetryWindow(seconds)).toBe(expected)
  })

  it('says `shortly` when the server sent no window', () => {
    expect(describeRetryWindow(null)).toBe('shortly')
    expect(describeRetryWindow(undefined)).toBe('shortly')
  })
})

describe('normaliseLane — cancelled (RAIL-22)', () => {
  it('reads `Cancelled.` and offers Run again', () => {
    const view = normaliseLane(toolInvocation({ status: 'cancelled' }))
    if (view.state !== 'cancelled') throw new Error('unreachable')
    expect(view.status).toBe(LANE_COPY.cancelled)
    expect(view.actions).toEqual(['run-again'])
  })
})

describe('normaliseLane — unknown is not NPC (X-8, RAIL-24)', () => {
  it('renders the neutral placeholder for an entry the client could not read', () => {
    expect(normaliseLane(null)).toEqual({ state: 'unreadable', placeholder: LANE_COPY.newerVersion })
  })

  it('renders the placeholder for a tool id this bundle does not know', () => {
    // The cast is the point: only a newer server can produce this, and it must
    // not become an NPC lane on the way in.
    const stranger = { ...toolInvocation(), tool_id: 'wardrobe' } as unknown as ToolInvocation
    const view = normaliseLane(stranger)
    expect(view.state).toBe('unreadable')
    expect(JSON.stringify(view)).not.toContain('npc')
  })
})

describe('laneChips — suggestions arm, never run (RAIL-8)', () => {
  it('keeps at most three, even when a server sends more', () => {
    const many: ToolSuggestion[] = [...SUGGESTIONS, { tool_id: 'loot', label: 'Roll its hoard', brief: null }]
    expect(laneChips(many, ALL_ENABLED)).toHaveLength(3)
  })

  it('hands back the canonical command, the brief and the source entry', () => {
    const [chip] = laneChips(SUGGESTIONS, ALL_ENABLED, 'ent_1')
    expect(chip.armed).toEqual({
      toolId: 'encounter',
      command: '/encounter',
      brief: 'an ambush at the crossing',
      sourceEntryId: 'ent_1',
    })
  })

  it('drops a suggestion whose target is capability-disabled', () => {
    const suggestions: ToolSuggestion[] = [{ tool_id: 'portrait', label: 'Portrait', icon: 'image', brief: null }]
    expect(laneChips(suggestions, DEFAULT_AVAILABILITY)).toEqual([])
  })

  it('drops a suggestion whose target is not a registry tool at all', () => {
    const suggestions = [{ tool_id: 'wardrobe', label: 'Open the wardrobe', brief: null }] as unknown as ToolSuggestion[]
    expect(laneChips(suggestions, ALL_ENABLED)).toEqual([])
  })

  it('offers nothing before the capability lookup has answered (AE-58)', () => {
    expect(laneChips(SUGGESTIONS)).toEqual([])
  })

  it('keeps counting to three AFTER dropping, so a disabled target costs no slot', () => {
    const suggestions: ToolSuggestion[] = [
      { tool_id: 'portrait', label: 'Portrait', brief: null },
      ...SUGGESTIONS,
    ]
    expect(laneChips(suggestions, DEFAULT_AVAILABILITY).map((chip) => chip.armed.toolId))
      .toEqual(['encounter', 'hooks', 'monster'])
  })

  it('drops a brief that is blank or over 200 characters, and keeps the chip', () => {
    const suggestions: ToolSuggestion[] = [
      { tool_id: 'hooks', label: 'Hooks', brief: '   ' },
      { tool_id: 'loot', label: 'Loot', brief: 'x'.repeat(201) },
    ]
    expect(laneChips(suggestions, ALL_ENABLED).map((chip) => chip.armed.brief)).toEqual([null, null])
  })

  it('trims a brief the server padded', () => {
    const suggestions: ToolSuggestion[] = [{ tool_id: 'hooks', label: 'Hooks', brief: '  a missing heir  ' }]
    expect(laneChips(suggestions, ALL_ENABLED)[0].armed.brief).toBe('a missing heir')
  })

  it('keys a chip by position, never by its text (X-7)', () => {
    const chips = laneChips(SUGGESTIONS, ALL_ENABLED, 'ent_1')
    expect(chips.map((chip) => chip.key)).toEqual(['0', '1', '2'])
  })

  it('reaches the lane through a done view, filtered the same way', () => {
    // The wire schema already caps the list at three (MAX_SUGGESTIONS), so what
    // a real payload exercises is the capability filter, not the cap.
    const done = toolInvocation({
      status: 'done',
      result: documentResult({
        suggestions: [{ tool_id: 'portrait', label: 'Portrait', brief: null }, SUGGESTIONS[0], SUGGESTIONS[1]],
      }),
    })
    const view = normaliseLane(done, { availability: DEFAULT_AVAILABILITY, sourceEntryId: 'ent_9' })
    if (view.state !== 'done') throw new Error('unreachable')
    expect(view.chips.map((chip) => chip.label)).toEqual([SUGGESTIONS[0].label, SUGGESTIONS[1].label])
    expect(view.chips[0].armed.sourceEntryId).toBe('ent_9')
  })
})

describe('documentLinkMeta (CANVAS-3)', () => {
  it('reads `<kind> · saved to <category>`', () => {
    expect(documentLinkMeta(DOCUMENT_LINK as never)).toBe('NPC Dossier · saved to NPCs')
  })

  it('returns null for a type this bundle does not know (X-8)', () => {
    const link = { ...DOCUMENT_LINK, type: 'grimoire' } as never
    expect(documentLinkMeta(link)).toBeNull()
  })
})

describe('result kinds the lane carries', () => {
  it('passes a card result through untouched', () => {
    const view = normaliseLane(toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() }))
    expect(view.state === 'done' && view.result.result_kind).toBe('card')
  })

  it('passes a media result through — the lane decides how to stand in for it', () => {
    const view = normaliseLane(toolInvocation({ tool_id: 'portrait', status: 'done', result: mediaResult() }))
    expect(view.state === 'done' && view.result.result_kind).toBe('media')
  })
})

describe('realTimer', () => {
  it('schedules and cancels without firing', async () => {
    let fired = false
    const cancel = realTimer.schedule(1, () => {
      fired = true
    })
    cancel()
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(fired).toBe(false)
  })

  it('fires when it is not cancelled', async () => {
    let fired = false
    realTimer.schedule(1, () => {
      fired = true
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(fired).toBe(true)
  })
})
