/**
 * tavernOrder -- the tavern's pure rules (agent-forge-harness-30c, PR-1).
 *
 * No React and no I/O: how the screen orders and pages the campaigns it was
 * given, the words it prints for a card's facts, and which started thread
 * "Where you were" offers. Everything here is a function of its arguments, so
 * the screen's tests can pin each rule without mounting anything.
 */
import type { ChatMode } from '../api'
import type { BadgeTone } from '../ds/Badge'
import type { Campaign, PlayerSeat } from '../gm/contracts'
import type { Conversation, ConversationStore } from './conversationStore'

/** Cards shown before "Show more campaigns" (ID-10), in the same step after. */
export const TAVERN_PAGE_SIZE = 12
/** A hard ceiling on first-visit page reads against a runaway cursor (ID-11).
 * The account cap is 100 campaigns, so a real read is two pages of fifty. */
export const TAVERN_MAX_PAGES = 4

export type GameSystem = Campaign['game_system']

/** The system chip's text (ID-12). */
export const SYSTEM_LABELS: Record<GameSystem, string> = { dnd5e: '5e' }

export const BADGE_COPY: Record<NonNullable<Campaign['badge']>, { readonly label: string; readonly tone: BadgeTone }> = {
  live: { label: 'LIVE', tone: 'nat20' },
  ready: { label: 'READY', tone: 'verdigris' },
}

/** The moment a timestamp names; one with an offset is read as that moment, so
 * `23:00+05:00` is earlier than `19:00Z` though it sorts later as text. */
function momentOf(iso: string): number {
  return Date.parse(iso)
}

function byRecency(a: Campaign, b: Campaign): number {
  return (
    momentOf(b.last_activity_at) - momentOf(a.last_activity_at)
    || momentOf(b.created_at) - momentOf(a.created_at)
    || (a.campaign_id < b.campaign_id ? -1 : a.campaign_id > b.campaign_id ? 1 : 0)
  )
}

/** Concluded campaigns apart from the rest; each group newest activity first,
 * then newest created, then id. A dormant campaign is not a third group: it
 * keeps its recency position among the active ones (ID-8). The client orders
 * on its own: the server's order is not trusted for recency. */
export function orderCampaigns(items: readonly Campaign[]): { active: Campaign[]; concluded: Campaign[] } {
  const active: Campaign[] = []
  const concluded: Campaign[] = []
  for (const item of items) (item.concluded_at === null ? active : concluded).push(item)
  return { active: active.sort(byRecency), concluded: concluded.sort(byRecency) }
}

const DAY_MONTH = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'long', timeZone: 'UTC' })
const DAY_MONTH_YEAR = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })

/** "4 March", or "4 March 2025" when the year is not `now`'s. Read in UTC so
 * the same stamp prints the same date on every machine. */
export function tavernDate(iso: string, now: Date): string {
  const at = new Date(momentOf(iso))
  return (at.getUTCFullYear() === now.getUTCFullYear() ? DAY_MONTH : DAY_MONTH_YEAR).format(at)
}

/** The card's seat chip text; none at zero (ID-12). */
export function seatChipLabel(count: number): string | null {
  if (count <= 0) return null
  return count === 1 ? '1 player' : `${count} players`
}

const RESUMABLE_MODES: readonly ChatMode[] = ['sage', 'spell', 'rules']

/** The newest started Sage, Spell or Rules thread in this browser's store, for
 * "Where you were" (ID-15). A GM thread belongs to a campaign, and a thread
 * with no first prompt is not a place anyone was. */
export function latestConversation(store: Pick<ConversationStore, 'list'>): Conversation | null {
  let newest: Conversation | null = null
  for (const mode of RESUMABLE_MODES) {
    for (const conversation of store.list(mode)) {
      if (!conversation.hasFirstPrompt) continue
      if (newest === null || momentOf(conversation.createdAt) > momentOf(newest.createdAt)) newest = conversation
    }
  }
  return newest
}

/** What a seat's recency is: when its table last met, else when it was taken. */
function seatMoment(seat: PlayerSeat): number {
  return momentOf(seat.last_played_at ?? seat.accepted_at)
}

/** A seat's place before recency: a table that is live for a confirmed seat
 * first (it is the one a player came for), a concluded table last (30c PR-2,
 * ID-23). */
function seatRank(seat: PlayerSeat): number {
  if (seat.concluded) return 2
  return seat.live && seat.confirmed ? 0 : 1
}

/** The caller's seats in the order the screen shows them: live first, concluded
 * last, each group by when its table last met (else when the seat was taken),
 * newest first, then newest acceptance, then campaign id. The client orders on
 * its own, as for campaigns: the server's order is by acceptance only. */
export function orderSeats(items: readonly PlayerSeat[]): PlayerSeat[] {
  return [...items].sort(
    (a, b) => (
      seatRank(a) - seatRank(b)
      || seatMoment(b) - seatMoment(a)
      || momentOf(b.accepted_at) - momentOf(a.accepted_at)
      || (a.campaign_id < b.campaign_id ? -1 : a.campaign_id > b.campaign_id ? 1 : 0)
    ),
  )
}
