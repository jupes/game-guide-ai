/**
 * The one slash parser (1kg.3.3) — record §4.
 *
 * SLASH-6: one pure function of the draft text, shared by the menu, the
 * armed-tool row and submit, so the rail, More, the slash menu and a suggestion
 * chip cannot disagree about what would run (RAIL-2). Nothing here reaches the
 * network: the only thing that starts billable work is `submitDraft` returning
 * `invoke`, and only an explicit submit calls it (X-1).
 *
 * The grammar is record §4.1:
 *
 *     draft   = [ws] "/" token [ ws+ brief ]
 *     token   = the maximal run of non-whitespace after "/"; it may be empty
 *     brief   = everything after the first whitespace run following the token,
 *               trimmed; may contain newlines and further "/" characters
 *     escape  = [ws] "//" rest   ; sends "/" + rest as a plain chat message
 *
 * Whitespace is JavaScript's `\s`, which is character-for-character the set
 * `trimWire` removes (contracts.ts) — so the client splits on exactly what the
 * server trims, and the two can never disagree about an empty brief.
 */

import { BRIEF_MAX_CHARS, codePointLength, trimWire } from './contracts'
import type { ToolId } from './contracts'
import { matchCommand } from './registry'
import type { Tool, ToolAvailability } from './registry'

/** SLASH-5: the escape hatch, so `//roll with it` can reach chat as `/roll with it`. */
export const ESCAPE = '//'

/** RAIL-5: what a `required` tool says when its brief is empty. */
export const BRIEF_HINT = 'Describe what you want — for example: /npc the hooded stranger at the bar'

/** RAIL-13 / SLASH-3: a known command with no campaign selected sends nothing. */
export const NO_CAMPAIGN_MESSAGE = 'Choose or create a campaign to use GM tools'

/** RAIL-3: the armed row's hint. */
export const ARMED_HINT = 'Describe it, then press Enter'

/**
 * A token can name a command only if it is ALPHA *31( ALPHA / DIGIT / "-" )
 * (§4.1). `/1d20+5` therefore never names a tool, however the registry grows.
 */
const COMMAND_TOKEN = /^[A-Za-z][A-Za-z0-9-]{0,31}$/

/** SLASH-4: a typo is never sent — not as a tool, and not as chat. */
export function unknownToolMessage(token: string): string {
  return `Unknown tool "/${token}". Type / to see every tool, or start with // to send this as a message.`
}

/** RAIL-6: the client blocks an over-length submit with a counter. */
export function briefCounterMessage(length: number): string {
  return `${length} of ${BRIEF_MAX_CHARS} characters — shorten the brief to send it.`
}

/** The five outcomes of SLASH-6, and nothing else. */
export type Parse =
  | { kind: 'plain'; text: string }
  | { kind: 'escaped'; text: string }
  /** No whitespace after the token yet. `tool` is the exact match when the token already equals a command or alias. */
  | { kind: 'partial'; token: string; tool: Tool | null }
  | { kind: 'command'; tool: Tool; brief: string }
  | { kind: 'unknown'; token: string; brief: string }

export interface ParseOptions {
  /**
   * CANVAS-22: parsing is switched off while an edit is armed, so a `/` typed
   * into an edit instruction stays text. Owned by `1kg.6.x`; this module only
   * takes the switch.
   */
  slashEnabled?: boolean
}

/** SLASH-2: exact, case-insensitive, against every command and alias. Prefix matching is the menu's (SLASH-9). */
function commandFor(token: string): Tool | null {
  if (!COMMAND_TOKEN.test(token)) return null
  return matchCommand(`/${token}`) ?? null
}

/** SLASH-6 — the one parser. Pure: same draft, same answer, no side effects. */
export function parseDraft(draft: string, options: ParseOptions = {}): Parse {
  const slashEnabled = options.slashEnabled ?? true
  // SLASH-1: a command is recognised only when `/` is the FIRST non-whitespace
  // character. `AC 15/16`, `and/or` and a URL are text.
  const body = draft.replace(/^\s+/, '')
  if (!slashEnabled || !body.startsWith('/')) return { kind: 'plain', text: trimWire(draft) }
  if (body.startsWith(ESCAPE)) return { kind: 'escaped', text: body.slice(1) }

  const rest = body.slice(1)
  const space = rest.search(/\s/)
  if (space === -1) return { kind: 'partial', token: rest, tool: commandFor(rest) }

  const token = rest.slice(0, space)
  const brief = trimWire(rest.slice(space))
  const tool = commandFor(token)
  return tool ? { kind: 'command', tool, brief } : { kind: 'unknown', token, brief }
}

/** The text a parse would contribute as a brief when a tool is armed over it (table 3.2). */
function briefOf(parse: Parse): string {
  switch (parse.kind) {
    case 'plain':
      return parse.text
    // The `//` was an escape for the chat path and means nothing inside a
    // brief, so the words survive without it (inferred; §3.2 is silent).
    case 'escaped':
      return trimWire(parse.text)
    // Only the command token is replaced, and a partial draft is nothing but a token.
    case 'partial':
      return ''
    case 'command':
    case 'unknown':
      return parse.brief
  }
}

/**
 * RAIL-1 and table 3.2: arming writes the tool's canonical command and one
 * space into the draft. RAIL-4 — it never discards typed text; only the command
 * token is replaced. The caret is the composer's job.
 */
export function armDraft(draft: string, tool: Tool): string {
  const brief = briefOf(parseDraft(draft))
  return brief === '' ? `${tool.command} ` : `${tool.command} ${brief}`
}

export type BlockedReason = 'unknown_tool' | 'no_campaign' | 'tool_disabled' | 'brief_required' | 'brief_too_long'

/** What an explicit submit does. `invoke` is the ONLY billable outcome (X-1). */
export type Submission =
  | { kind: 'none' }
  | { kind: 'chat'; text: string }
  | { kind: 'invoke'; tool: Tool; brief: string }
  | { kind: 'blocked'; reason: BlockedReason; message: string; tool: Tool | null }

export interface SubmitContext extends ParseOptions {
  /** RAIL-10, from `toolAvailability`. */
  availability: Readonly<Record<ToolId, ToolAvailability>>
  /**
   * RAIL-13: whether a campaign is selected. The campaign model arrives with
   * `1kg.2.5`; until then the caller answers.
   */
  campaignSelected: boolean
}

function blocked(reason: BlockedReason, message: string, tool: Tool | null): Submission {
  return { kind: 'blocked', reason, message, tool }
}

/** The order is the record's: the parse first, then the campaign (SLASH-3), then
 * the capability (RAIL-10), then the brief (RAIL-5, RAIL-6). */
function invocation(tool: Tool, brief: string, context: SubmitContext): Submission {
  if (!context.campaignSelected) return blocked('no_campaign', NO_CAMPAIGN_MESSAGE, tool)
  const availability = context.availability[tool.id]
  if (!availability?.enabled) {
    return blocked('tool_disabled', availability?.reason ?? 'That tool is unavailable.', tool)
  }
  const length = codePointLength(brief)
  if (length === 0 && tool.brief === 'required') return blocked('brief_required', BRIEF_HINT, tool)
  if (length > BRIEF_MAX_CHARS) return blocked('brief_too_long', briefCounterMessage(length), tool)
  return { kind: 'invoke', tool, brief }
}

/**
 * What an explicit submit (Enter or Send) does with this draft — §4.2.
 *
 * Call it only when the slash menu is not accepting the active option: Enter
 * never both picks and submits (SLASH-11). A `partial` draft whose token is
 * already a command therefore runs with an empty brief, which is the second
 * Enter of `/recap`.
 */
export function submitDraft(draft: string, context: SubmitContext): Submission {
  const parse = parseDraft(draft, context)
  switch (parse.kind) {
    case 'plain':
    case 'escaped':
      return parse.text === '' ? { kind: 'none' } : { kind: 'chat', text: parse.text }
    case 'partial':
      return parse.tool ? invocation(parse.tool, '', context) : blocked('unknown_tool', unknownToolMessage(parse.token), null)
    case 'unknown':
      return blocked('unknown_tool', unknownToolMessage(parse.token), null)
    case 'command':
      return invocation(parse.tool, parse.brief, context)
  }
}

/** The tool the armed-tool row names (RAIL-3): the draft parses as a known command. */
export function armedTool(parse: Parse): Tool | null {
  return parse.kind === 'command' ? parse.tool : null
}

/** RAIL-6, for the live counter: how long this draft's brief is, in code points. */
export function briefLength(parse: Parse): number {
  return parse.kind === 'command' ? codePointLength(parse.brief) : 0
}
