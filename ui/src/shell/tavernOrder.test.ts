/**
 * tavernOrder.test.ts -- the tavern's pure rules (agent-forge-harness-30c,
 * PR-1, U-1 to U-7): ordering, the date line, the seat chip, the badge copy and
 * the "where you were" pick.
 */
import { describe, expect, it } from 'vitest'
import type { ChatMode } from '../api'
import type { Campaign } from '../gm/contracts'
import type { Conversation } from './conversationStore'
import {
  BADGE_COPY,
  SYSTEM_LABELS,
  TAVERN_MAX_PAGES,
  TAVERN_PAGE_SIZE,
  latestConversation,
  orderCampaigns,
  seatChipLabel,
  tavernDate,
} from './tavernOrder'

function campaign(id: string, over: Partial<Campaign> = {}): Campaign {
  return {
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-01T12:00:00Z',
    updated_at: '2026-09-01T12:00:00Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-01T12:00:00Z', last_played_at: null, dormant: false, ...over,
  }
}
const ids = (items: readonly Campaign[]): string[] => items.map((c) => c.campaign_id)

describe('orderCampaigns (U-1 to U-3)', () => {
  it('U-1 sorts the active group by last activity, newest first, whatever order it arrives in', () => {
    const a = campaign('cmp_a', { last_activity_at: '2026-09-01T12:00:00Z' })
    const b = campaign('cmp_b', { last_activity_at: '2026-09-03T12:00:00Z' })
    const c = campaign('cmp_c', { last_activity_at: '2026-09-02T12:00:00Z' })
    expect(ids(orderCampaigns([a, b, c]).active)).toEqual(['cmp_b', 'cmp_c', 'cmp_a'])
    expect(ids(orderCampaigns([c, a, b]).active)).toEqual(['cmp_b', 'cmp_c', 'cmp_a'])
  })

  it('U-1 reads an offset timestamp as the moment it names, not as text', () => {
    // 23:00 at +05:00 is 18:00Z, earlier than 19:00Z though it sorts later as text.
    const early = campaign('cmp_early', { last_activity_at: '2026-09-01T23:00:00+05:00' })
    const late = campaign('cmp_late', { last_activity_at: '2026-09-01T19:00:00Z' })
    expect(ids(orderCampaigns([early, late]).active)).toEqual(['cmp_late', 'cmp_early'])
  })

  it('U-2 breaks a tie on created_at (newest first), then on the id (ascending)', () => {
    const same = '2026-09-02T12:00:00Z'
    const older = campaign('cmp_y', { last_activity_at: same, created_at: '2026-08-01T12:00:00Z' })
    const newer = campaign('cmp_z', { last_activity_at: same, created_at: '2026-08-02T12:00:00Z' })
    expect(ids(orderCampaigns([older, newer]).active)).toEqual(['cmp_z', 'cmp_y'])
    const first = campaign('cmp_a', { last_activity_at: same, created_at: '2026-08-02T12:00:00Z' })
    expect(ids(orderCampaigns([newer, first]).active)).toEqual(['cmp_a', 'cmp_z'])
  })

  it('U-3 partitions on concluded_at; a dormant campaign stays active at its recency position', () => {
    const live = campaign('cmp_live', { last_activity_at: '2026-09-05T12:00:00Z' })
    const dormant = campaign('cmp_dormant', { dormant: true, last_activity_at: '2026-07-01T12:00:00Z' })
    const done = campaign('cmp_done', { concluded_at: '2026-09-06T12:00:00Z', last_activity_at: '2026-09-07T12:00:00Z' })
    const doneDormant = campaign('cmp_done2', {
      concluded_at: '2026-09-06T12:00:00Z', dormant: true, last_activity_at: '2026-06-01T12:00:00Z',
    })
    const { active, concluded } = orderCampaigns([done, dormant, doneDormant, live])
    expect(ids(active)).toEqual(['cmp_live', 'cmp_dormant'])
    expect(ids(concluded)).toEqual(['cmp_done', 'cmp_done2'])
  })

  it('does not change the list it is given', () => {
    const list = [campaign('cmp_a'), campaign('cmp_b', { last_activity_at: '2026-09-09T12:00:00Z' })]
    const copy = [...list]
    orderCampaigns(list)
    expect(list).toEqual(copy)
  })
})

describe('seatChipLabel (U-4)', () => {
  it('is null for none, singular for one and plural after', () => {
    expect(seatChipLabel(0)).toBeNull()
    expect(seatChipLabel(1)).toBe('1 player')
    expect(seatChipLabel(2)).toBe('2 players')
    expect(seatChipLabel(4)).toBe('4 players')
  })
})

describe('tavernDate (U-5)', () => {
  const now = new Date('2026-10-01T09:00:00Z')
  it('omits the year in the current year', () => {
    expect(tavernDate('2026-03-04T12:00:00Z', now)).toBe('4 March')
  })
  it('names the year for an earlier one', () => {
    expect(tavernDate('2025-03-04T12:00:00Z', now)).toBe('4 March 2025')
  })
  it('keeps the first of the month a single digit and reads the UTC date', () => {
    expect(tavernDate('2026-01-01T00:30:00Z', now)).toBe('1 January')
    expect(tavernDate('2025-12-31T23:30:00Z', now)).toBe('31 December 2025')
  })
})

function row(id: string, mode: ChatMode, createdAt: string, hasFirstPrompt: boolean): Conversation {
  return {
    id, mode, title: id, derivedTitle: id, customTitle: null, hasFirstPrompt, createdAt,
    modelPreference: 'auto', boundPreference: hasFirstPrompt ? 'auto' : null,
  }
}
/** `latestConversation` reads only `list(mode)`, so the store here is just that. */
function storeOf(rows: Conversation[]): { list: (mode: ChatMode) => Conversation[] } {
  return { list: (mode) => rows.filter((r) => r.mode === mode) }
}

describe('latestConversation (U-6)', () => {
  const stamp = (n: number): string => `2026-09-0${n}T12:00:00.000Z`
  it('picks the newest started sage, spell or rules row, ignoring gm and unstarted rows', () => {
    const store = storeOf([
      row('cnv_gm', 'gm', stamp(9), true),
      row('cnv_unstarted', 'sage', stamp(8), false),
      row('cnv_sage', 'sage', stamp(1), true),
      row('cnv_spell', 'spell', stamp(3), true),
      row('cnv_rules', 'rules', stamp(2), true),
    ])
    expect(latestConversation(store)?.id).toBe('cnv_spell')
  })
  it('returns null when none qualifies', () => {
    expect(latestConversation(storeOf([]))).toBeNull()
    expect(latestConversation(storeOf([row('cnv_gm', 'gm', stamp(9), true)]))).toBeNull()
    expect(latestConversation(storeOf([row('cnv_u', 'sage', stamp(9), false)]))).toBeNull()
  })
})

describe('constants (U-7)', () => {
  it('maps the two badges to their copy and tone', () => {
    expect(BADGE_COPY.live).toEqual({ label: 'LIVE', tone: 'nat20' })
    expect(BADGE_COPY.ready).toEqual({ label: 'READY', tone: 'verdigris' })
  })
  it('names the system "5e" and sets the paging limits', () => {
    expect(SYSTEM_LABELS.dnd5e).toBe('5e')
    expect(TAVERN_PAGE_SIZE).toBe(12)
    expect(TAVERN_MAX_PAGES).toBe(4)
  })
})
