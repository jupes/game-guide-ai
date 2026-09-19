/**
 * The assistant lane's state machine (1kg.3.2) — everything the lane decides,
 * as pure functions with no React in sight.
 *
 * The handoff's `AssistantLane` takes `status` and `children` as unrelated
 * props, so a `working` header can sit above the previous result's card. The
 * record forbids exactly that (RAIL-15, RAIL-18: "No children render"), so the
 * fix here is a type rather than a convention: `LaneView` is a discriminated
 * union in which only the `done` member carries a result or suggestions. A
 * working or failed lane has nowhere to put a stale child.
 *
 * `normaliseLane` is the only way to build a view, and it is a pure function of
 * a wire `ToolInvocation` plus the two things the client knows and the server
 * does not: whether the entry was hydrated or its response lost (RAIL-21), and
 * whether this client's own 30 s timer has fired (RAIL-15). Both arrive as
 * values, so tests never wait and the component never needs a clock of its own
 * beyond the injectable timer below.
 *
 * Privacy (X-7): nothing here writes to storage, a URL or a log. A brief is
 * carried from a suggestion to the caller in memory and nowhere else, and a
 * chip's React key is its position, never its text.
 */

import { MAX_SUGGESTIONS, SUGGESTION_BRIEF_MAX_CHARS, codePointLength } from './contracts'
import type {
  DocumentLink,
  ErrorInfo,
  LibraryCategory,
  ToolId,
  ToolInvocation,
  ToolResult,
  ToolSuggestion,
} from './contracts'
import { documentTypeById, toolById } from './registry'
import type { Tool, ToolAvailability } from './registry'

/** RAIL-15: after 30 s the working label becomes `Still working…`. */
export const STILL_WORKING_MS = 30_000

/**
 * Every sentence the lane can say, in the record's words. Kept in one object so
 * a reviewer can diff copy against §3.4 and §5.1 without reading JSX.
 */
export const LANE_COPY = {
  /** RAIL-15 */
  stillWorking: 'Still working…',
  /** RAIL-23 */
  cancelling: 'Cancelling…',
  /** RAIL-22 */
  cancelled: 'Cancelled.',
  /** RAIL-23 — the provider had already finished, so the result is kept. */
  lateFinish: 'Finished before it could be cancelled',
  /** RAIL-20, the pilot's daily cap. No retry is offered. */
  dailyCap: "The pilot's daily limit is spent. It resets overnight.",
  /** RAIL-24, X-8 — never a crash, never an NPC. */
  newerVersion: 'This result was made by a newer version of Aetheril.',
  /** A result kind this bead does not own (the media card is 1kg.8.4's). */
  unsupportedResult: "This result can't be shown here yet.",
  /** CANVAS-3 */
  openInCanvas: 'Open in canvas',
  /** The lane's kicker, from the handoff. */
  author: 'Aetheril · Assistant',
} as const

/** The actions of the §3.4 state table, with the record's exact button copy. */
export type LaneActionId = 'cancel' | 'try-again' | 'edit-brief' | 'check-again' | 'run-again'

export const LANE_ACTION_LABEL: Readonly<Record<LaneActionId, string>> = {
  cancel: 'Cancel',
  'try-again': 'Try again',
  'edit-brief': 'Edit brief',
  'check-again': 'Check again',
  'run-again': 'Run again',
}

/** §6.1 — the five library categories by their supplied names (LIB-1 to LIB-5). */
export const LIBRARY_CATEGORY_LABEL: Readonly<Record<LibraryCategory, string>> = {
  npcs: 'NPCs',
  bestiary: 'Bestiary',
  documents: 'Documents',
  'session-log': 'Session log',
  cues: 'Cues',
}

/**
 * CANVAS-3's subtitle: `<kind> · saved to <category>`, both names from the
 * registry. `null` for a document type this bundle does not know, so the caller
 * shows a neutral placeholder rather than guessing a type (X-8).
 */
export function documentLinkMeta(link: DocumentLink): string | null {
  const type = documentTypeById(link.type)
  if (!type) return null
  return `${type.label} · saved to ${LIBRARY_CATEGORY_LABEL[link.library_category]}`
}

// ── The injectable clock ─────────────────────────────────────────────────────

/**
 * The one piece of time the lane owns (RAIL-15's 30 s). It is a port rather
 * than a bare `setTimeout` so a test drives it by hand instead of waiting half
 * a minute, and so a future host (a timeline that hydrates a hundred lanes) can
 * share one scheduler.
 */
export interface LaneTimer {
  /** Runs `fn` once, `ms` from now. Returns a function that cancels it. */
  schedule(ms: number, fn: () => void): () => void
}

export const realTimer: LaneTimer = {
  schedule(ms, fn) {
    const handle = setTimeout(fn, ms)
    return () => {
      clearTimeout(handle)
    }
  },
}

// ── Suggestions (RAIL-8) ─────────────────────────────────────────────────────

/**
 * What a chip hands back. It is everything the composer needs to be *armed* and
 * nothing that could run anything: the target command, an optional prefilled
 * brief and the entry the suggestion came from (X-1, RAIL-8).
 */
export interface ArmedSuggestion {
  toolId: ToolId
  /** The tool's canonical command, e.g. `/encounter`. Never the label. */
  command: string
  /** The server's prefilled brief, trimmed, or `null` when there is none. */
  brief: string | null
  /** The timeline entry whose result offered this suggestion. */
  sourceEntryId: string | null
}

export interface LaneChip {
  /** React key. The suggestion's position — never its text (X-7). */
  key: string
  label: string
  icon?: string
  armed: ArmedSuggestion
}

/** No capability answer yet, or the lookup failed: offer nothing (AE-58, X-8). */
const NOTHING_ENABLED: Readonly<Partial<Record<ToolId, ToolAvailability>>> = {}

/**
 * RAIL-8: at most three suggestions render, and one whose target is not a
 * known, *enabled* registry tool is dropped. The server drops them too; this is
 * the client repeating the check rather than trusting it, because a chip for a
 * tool that cannot run is a dead end (STATE-2).
 *
 * An over-long or blank prefilled brief costs the chip its brief, not its
 * existence: the brief is never shown, so dropping it changes nothing the GM
 * can see, and the chip still arms the command.
 */
export function laneChips(
  suggestions: readonly ToolSuggestion[],
  availability: Readonly<Partial<Record<ToolId, ToolAvailability>>> = NOTHING_ENABLED,
  sourceEntryId: string | null = null,
): LaneChip[] {
  const chips: LaneChip[] = []
  suggestions.forEach((suggestion, index) => {
    if (chips.length === MAX_SUGGESTIONS) return
    const tool = toolById(suggestion.tool_id)
    if (!tool) return
    if (availability[suggestion.tool_id]?.enabled !== true) return
    chips.push({
      key: String(index),
      label: suggestion.label,
      ...(suggestion.icon ? { icon: suggestion.icon } : {}),
      armed: {
        toolId: tool.id,
        command: tool.command,
        brief: usableBrief(suggestion.brief),
        sourceEntryId,
      },
    })
  })
  return chips
}

function usableBrief(brief: string | null | undefined): string | null {
  const trimmed = (brief ?? '').trim()
  if (trimmed === '') return null
  return codePointLength(trimmed) > SUGGESTION_BRIEF_MAX_CHARS ? null : trimmed
}

// ── Errors (RAIL-18 to RAIL-20) ──────────────────────────────────────────────

/**
 * RAIL-20's per-user window, phrased from `Retry-After`. The record fixes the
 * sentence and leaves `<when>` open; this is the smallest reading of it.
 */
export function describeRetryWindow(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'shortly'
  if (seconds <= 0) return 'now'
  if (seconds < 60) return `in ${seconds} ${seconds === 1 ? 'second' : 'seconds'}`
  const minutes = Math.ceil(seconds / 60)
  return `in ${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`
}

interface ErrorView {
  message: string
  actions: readonly LaneActionId[]
}

/**
 * The three failure rows of §3.4, told apart by the error code the wire
 * contract closes over — never by HTTP status, which the lane never sees.
 */
function errorView(error: ErrorInfo): ErrorView {
  // RAIL-20, pilot daily cap: there is no window to wait out, so no retry.
  if (error.code === 'throttled_daily') return { message: LANE_COPY.dailyCap, actions: [] }
  // RAIL-20, per-user window.
  if (error.code === 'throttled_user') {
    return {
      message: `That's a lot at once — try again ${describeRetryWindow(error.retry_after_s)}`,
      actions: ['try-again'],
    }
  }
  // RAIL-18 (retryable, including an unmarked platform 429 and RAIL-27's
  // expired attempt) versus RAIL-19 (final: validation, a disabled tool, a
  // brief that is too long — all of them things an edited brief can answer).
  return { message: error.message, actions: error.retryable ? ['try-again'] : ['edit-brief'] }
}

// ── The view ─────────────────────────────────────────────────────────────────

interface WithTool {
  tool: Tool
}

/**
 * What the lane renders. Only `done` carries a result or suggestions, which is
 * what makes RAIL-15's and RAIL-18's "no children render" a compile error
 * rather than a code-review note.
 */
export type LaneView =
  | (WithTool & {
      state: 'working'
      /** The tool's working label, `Still working…`, or `Cancelling…`. */
      status: string
      /** RAIL-23: a cancel has been asked for and no terminal state is known. */
      cancelling: boolean
      actions: readonly LaneActionId[]
    })
  | (WithTool & { state: 'checking'; status: string; actions: readonly LaneActionId[] })
  | (WithTool & {
      state: 'done'
      /** RAIL-17's `<label> finished`, announced once, plus RAIL-23's note when there is one. */
      announcement: string
      /** RAIL-23's note as a visible line, when the provider beat the cancel. */
      note: string | null
      result: ToolResult
      chips: readonly LaneChip[]
    })
  | (WithTool & { state: 'error'; message: string; actions: readonly LaneActionId[] })
  | (WithTool & { state: 'cancelled'; status: string; actions: readonly LaneActionId[] })
  | { state: 'unreadable'; placeholder: string }

export interface LaneOptions {
  /** RAIL-21: a stored `working` entry hydrated after a reload is `unknown`. */
  hydrated?: boolean
  /** RAIL-21: the response was lost, or the client waited past 120 s. */
  lost?: boolean
  /** RAIL-15: this client's own 30 s timer has fired. */
  stillWorking?: boolean
  /** RAIL-8: which tools a suggestion may target. Absent means none. */
  availability?: Readonly<Partial<Record<ToolId, ToolAvailability>>>
  /** RAIL-8: the entry a suggestion would cite as its source. */
  sourceEntryId?: string | null
}

const UNREADABLE: LaneView = { state: 'unreadable', placeholder: LANE_COPY.newerVersion }

/**
 * The §3.4 table as one pure function. `null` stands for a stored entry this
 * client could not read at all — the `opaque` timeline entry, or a payload that
 * failed `parseToolInvocation` — and becomes RAIL-24's placeholder.
 */
export function normaliseLane(invocation: ToolInvocation | null, options: LaneOptions = {}): LaneView {
  if (invocation === null) return UNREADABLE
  const tool = toolById(invocation.tool_id)
  // X-8: an id this bundle does not know is neutral, never NPC.
  if (!tool) return UNREADABLE

  switch (invocation.status) {
    case 'working': {
      // RAIL-21: a working entry the client has lost track of is `unknown`. It
      // is checked on, never re-run, so it cannot bill twice.
      if (options.hydrated === true || options.lost === true) {
        return {
          state: 'checking',
          tool,
          status: `Checking on ${tool.label}…`,
          actions: ['check-again', 'cancel'],
        }
      }
      const cancelling = invocation.cancel_requested
      return {
        state: 'working',
        tool,
        status: cancelling
          ? LANE_COPY.cancelling
          : options.stillWorking === true
            ? LANE_COPY.stillWorking
            : tool.working_label,
        cancelling,
        actions: ['cancel'],
      }
    }
    case 'done': {
      // The schema pairs `done` with a result; a server that breaks that
      // promise gets the neutral placeholder rather than a blank lane.
      if (invocation.result === null) return UNREADABLE
      // STATE-7 rations announcements to one per finish, so the late-cancel
      // note rides along with it rather than firing a second time.
      return {
        state: 'done',
        tool,
        announcement: invocation.cancel_requested
          ? `${tool.label} finished. ${LANE_COPY.lateFinish}`
          : `${tool.label} finished`,
        note: invocation.cancel_requested ? LANE_COPY.lateFinish : null,
        result: invocation.result,
        chips: laneChips(invocation.result.suggestions, options.availability, options.sourceEntryId ?? null),
      }
    }
    case 'failed': {
      if (invocation.error === null) return UNREADABLE
      const { message, actions } = errorView(invocation.error)
      return { state: 'error', tool, message, actions }
    }
    case 'cancelled':
      return { state: 'cancelled', tool, status: LANE_COPY.cancelled, actions: ['run-again'] }
  }
}
