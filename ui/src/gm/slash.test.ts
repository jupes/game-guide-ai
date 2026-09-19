/**
 * The slash parser (1kg.3.3) — record §4.1, §4.2, and the arming table §3.2.
 *
 * Every row of §4.2 is a case here, named after it, because that table is the
 * contract three surfaces share (SLASH-6).
 */

import { describe, it, expect } from 'vitest'
import { BRIEF_MAX_CHARS } from './contracts'
import { REGISTRY, toolAvailability, toolById } from './registry'
import type { Tool } from './registry'
import {
  BRIEF_HINT,
  NO_CAMPAIGN_MESSAGE,
  armDraft,
  armedTool,
  briefLength,
  parseDraft,
  submitDraft,
  unknownToolMessage,
} from './slash'
import type { SubmitContext } from './slash'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

function context(overrides: Partial<SubmitContext> = {}): SubmitContext {
  return { availability: ALL_ENABLED, campaignSelected: true, ...overrides }
}

function tool(id: string): Tool {
  const found = toolById(id)
  if (!found) throw new Error(`no such tool: ${id}`)
  return found
}

// ── §4.2, the parse outcomes ─────────────────────────────────────────────────

describe('parseDraft — record §4.2', () => {
  it('`what does she want?` is plain', () => {
    expect(parseDraft('what does she want?')).toEqual({ kind: 'plain', text: 'what does she want?' })
  })

  it('`AC 15/16 seems high` is plain — a slash that is not first is text (SLASH-1)', () => {
    expect(parseDraft('AC 15/16 seems high').kind).toBe('plain')
    expect(parseDraft('and/or').kind).toBe('plain')
    expect(parseDraft('see https://example.test/npc').kind).toBe('plain')
  })

  it('`/` is partial with an empty token', () => {
    expect(parseDraft('/')).toEqual({ kind: 'partial', token: '', tool: null })
  })

  it('`/mo` is partial with no exact match', () => {
    expect(parseDraft('/mo')).toEqual({ kind: 'partial', token: 'mo', tool: null })
  })

  it('`/recap` is partial and carries the exact match', () => {
    const parse = parseDraft('/recap')
    expect(parse.kind).toBe('partial')
    expect(parse.kind === 'partial' && parse.tool?.id).toBe('recap')
  })

  it('`/recap ` is a command with an empty brief', () => {
    expect(parseDraft('/recap ')).toEqual({ kind: 'command', tool: tool('recap'), brief: '' })
  })

  it('`/npcs` is partial with no match — the menu shows its no-match row', () => {
    expect(parseDraft('/npcs')).toEqual({ kind: 'partial', token: 'npcs', tool: null })
  })

  it('`/1d20+5` is partial and the token cannot name a command', () => {
    expect(parseDraft('/1d20+5')).toEqual({ kind: 'partial', token: '1d20+5', tool: null })
  })

  it('a newline after the command is whitespace: `/recap\\nfocus on the heist`', () => {
    expect(parseDraft('/recap\nfocus on the heist')).toEqual({
      kind: 'command',
      tool: tool('recap'),
      brief: 'focus on the heist',
    })
  })

  it('`/MONSTER ` matches case-insensitively (SLASH-2)', () => {
    expect(parseDraft('/MONSTER ')).toEqual({ kind: 'command', tool: tool('monster'), brief: '' })
  })

  it('`/monster CR 5, drowned` carries its brief', () => {
    expect(parseDraft('/monster CR 5, drowned')).toEqual({
      kind: 'command',
      tool: tool('monster'),
      brief: 'CR 5, drowned',
    })
  })

  it('`/hooks a missing heir` matches through the alias (SLASH-2)', () => {
    expect(parseDraft('/hooks a missing heir')).toEqual({
      kind: 'command',
      tool: tool('hooks'),
      brief: 'a missing heir',
    })
  })

  it('`/foo bar` is unknown, and keeps the brief it would have had', () => {
    expect(parseDraft('/foo bar')).toEqual({ kind: 'unknown', token: 'foo', brief: 'bar' })
  })

  it('`//shrug` is escaped as `/shrug` (SLASH-5)', () => {
    expect(parseDraft('//shrug')).toEqual({ kind: 'escaped', text: '/shrug' })
    expect(parseDraft('//roll with it')).toEqual({ kind: 'escaped', text: '/roll with it' })
  })
})

describe('parseDraft — the grammar of §4.1', () => {
  it('allows leading whitespace before the slash', () => {
    expect(parseDraft('   /npc a ferryman')).toEqual({ kind: 'command', tool: tool('npc'), brief: 'a ferryman' })
  })

  it('takes the brief after the FIRST whitespace run, trimmed', () => {
    expect(parseDraft('/npc \t  the hooded stranger   ')).toEqual({
      kind: 'command',
      tool: tool('npc'),
      brief: 'the hooded stranger',
    })
  })

  it('lets a brief hold newlines and further slashes', () => {
    const parse = parseDraft('/npc a ferryman\nwho knows the tide\nAC 15/16')
    expect(parse).toEqual({ kind: 'command', tool: tool('npc'), brief: 'a ferryman\nwho knows the tide\nAC 15/16' })
  })

  it('refuses a token that does not start with a letter, however the registry grows', () => {
    expect(parseDraft('/2nd wind').kind).toBe('unknown')
    expect(parseDraft('/-npc x').kind).toBe('unknown')
  })

  it('refuses a token longer than 32 characters', () => {
    expect(parseDraft(`/${'n'.repeat(33)} x`)).toEqual({ kind: 'unknown', token: 'n'.repeat(33), brief: 'x' })
  })

  it('is switched off while an edit is armed (CANVAS-22, SLASH-6)', () => {
    expect(parseDraft('/npc a ferryman', { slashEnabled: false })).toEqual({
      kind: 'plain',
      text: '/npc a ferryman',
    })
  })
})

// ── §3.2, arming the composer ────────────────────────────────────────────────

describe('armDraft — record table 3.2', () => {
  const npc = tool('npc')

  it('an empty or whitespace draft becomes `/npc `', () => {
    expect(armDraft('', npc)).toBe('/npc ')
    expect(armDraft('   \n ', npc)).toBe('/npc ')
  })

  it('existing text becomes the brief (RAIL-4 — nothing typed is discarded)', () => {
    expect(armDraft('the hooded stranger', npc)).toBe('/npc the hooded stranger')
  })

  it('only the command token is replaced', () => {
    expect(armDraft('/monster a drowned thing', npc)).toBe('/npc a drowned thing')
  })

  it('a partial token is replaced and nothing is kept', () => {
    expect(armDraft('/mon', npc)).toBe('/npc ')
  })

  it('an unknown command token is replaced, and its brief survives', () => {
    expect(armDraft('/foo bar', npc)).toBe('/npc bar')
  })

  it('keeps the words of an escaped draft, without the escape', () => {
    expect(armDraft('//shrug', npc)).toBe('/npc /shrug')
  })

  it('re-arming a command with an empty brief leaves the trailing space', () => {
    expect(armDraft('/monster ', npc)).toBe('/npc ')
  })
})

// ── X-1, the submit gate ─────────────────────────────────────────────────────

describe('submitDraft — the only path to an invocation (X-1)', () => {
  it('sends a plain draft to chat, not to a tool (RAIL-14)', () => {
    expect(submitDraft('what does she want?', context())).toEqual({
      kind: 'chat',
      text: 'what does she want?',
    })
  })

  it('does nothing at all for an empty draft', () => {
    expect(submitDraft('   ', context())).toEqual({ kind: 'none' })
  })

  it('sends an escaped draft as a message (SLASH-5)', () => {
    expect(submitDraft('//shrug', context())).toEqual({ kind: 'chat', text: '/shrug' })
  })

  it('AE-5: `/monster CR 5, drowned` invokes with tool and brief', () => {
    expect(submitDraft('/monster CR 5, drowned', context())).toEqual({
      kind: 'invoke',
      tool: tool('monster'),
      brief: 'CR 5, drowned',
    })
  })

  it('AE-4: `/recap ` invokes with an empty brief, because recap is brief-optional (RAIL-5)', () => {
    expect(submitDraft('/recap ', context())).toEqual({ kind: 'invoke', tool: tool('recap'), brief: '' })
  })

  it('a partial draft whose token is already a command runs it — the second Enter of `/recap`', () => {
    expect(submitDraft('/recap', context())).toEqual({ kind: 'invoke', tool: tool('recap'), brief: '' })
  })

  it('AE-3: a required tool with an empty brief is blocked, never sent (RAIL-5)', () => {
    expect(submitDraft('/npc ', context())).toEqual({
      kind: 'blocked',
      reason: 'brief_required',
      message: BRIEF_HINT,
      tool: tool('npc'),
    })
  })

  it('AE-7: an unknown command is never sent, and the message mentions `//` (SLASH-4)', () => {
    const outcome = submitDraft('/npcs a guard', context())
    expect(outcome).toEqual({
      kind: 'blocked',
      reason: 'unknown_tool',
      message: unknownToolMessage('npcs'),
      tool: null,
    })
    expect(outcome.kind === 'blocked' && outcome.message).toContain('//')
  })

  it('a token that cannot name a command is the same unknown-tool refusal', () => {
    expect(submitDraft('/1d20+5', context()).kind).toBe('blocked')
  })

  it('a capability-disabled tool is refused with its reason (RAIL-10)', () => {
    expect(submitDraft('/portrait Ondrey', context({ availability: DEFAULTS }))).toEqual({
      kind: 'blocked',
      reason: 'tool_disabled',
      message: "Image generation isn't set up yet.",
      tool: tool('portrait'),
    })
  })

  it('AE-58: the capability lookup having failed refuses every tool with one message', () => {
    const outcome = submitDraft('/npc the ferryman', context({ availability: toolAvailability(null) }))
    expect(outcome).toEqual({
      kind: 'blocked',
      reason: 'tool_disabled',
      message: "Couldn't check which tools are available",
      tool: tool('npc'),
    })
  })

  it('with no campaign selected a known command sends nothing (SLASH-3, RAIL-13)', () => {
    expect(submitDraft('/npc the ferryman', context({ campaignSelected: false }))).toEqual({
      kind: 'blocked',
      reason: 'no_campaign',
      message: NO_CAMPAIGN_MESSAGE,
      tool: tool('npc'),
    })
  })

  it('with no campaign, plain chat still works — uncampaigned GM chat is unchanged', () => {
    expect(submitDraft('who runs the docks?', context({ campaignSelected: false }))).toEqual({
      kind: 'chat',
      text: 'who runs the docks?',
    })
  })

  it('blocks an over-length brief with a counter before anything is sent (RAIL-6)', () => {
    const outcome = submitDraft(`/npc ${'a'.repeat(BRIEF_MAX_CHARS + 1)}`, context())
    expect(outcome.kind).toBe('blocked')
    expect(outcome.kind === 'blocked' && outcome.reason).toBe('brief_too_long')
    expect(outcome.kind === 'blocked' && outcome.message).toContain(String(BRIEF_MAX_CHARS + 1))
  })

  it('allows a brief of exactly the maximum', () => {
    expect(submitDraft(`/npc ${'a'.repeat(BRIEF_MAX_CHARS)}`, context()).kind).toBe('invoke')
  })

  it('counts a brief in code points, as the server does', () => {
    // '🎲'.length is 2; the wire counts it as 1.
    const outcome = submitDraft(`/npc ${'🎲'.repeat(BRIEF_MAX_CHARS)}`, context())
    expect(outcome.kind).toBe('invoke')
  })

  it('never invokes while slash parsing is off — an edit instruction is not a command', () => {
    expect(submitDraft('/npc make her older', context({ slashEnabled: false }))).toEqual({
      kind: 'chat',
      text: '/npc make her older',
    })
  })
})

describe('the helpers the composer reads', () => {
  it('armedTool names a tool only once the draft parses as a command (RAIL-3)', () => {
    expect(armedTool(parseDraft('/npc'))).toBeNull()
    expect(armedTool(parseDraft('/npc '))?.id).toBe('npc')
    expect(armedTool(parseDraft('the hooded stranger'))).toBeNull()
  })

  it('briefLength counts only a command draft, in code points', () => {
    expect(briefLength(parseDraft('/npc 🎲🎲'))).toBe(2)
    expect(briefLength(parseDraft('not a command'))).toBe(0)
  })

  it('every registry command and alias parses to its own tool (SLASH-2, one registry)', () => {
    for (const registered of REGISTRY.tools) {
      for (const command of [registered.command, ...registered.aliases]) {
        const parse = parseDraft(`${command} a brief`)
        expect(parse.kind === 'command' && parse.tool.id).toBe(registered.id)
      }
    }
  })
})
