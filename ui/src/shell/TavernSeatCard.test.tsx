/**
 * TavernSeatCard.test.tsx -- one of the caller's own seats on the tavern screen
 * (agent-forge-harness-30c, PR-2, C-9 to C-14; ID-17, ID-24).
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { PlayerSeat } from '../gm/contracts'
import { TavernSeatCard } from './TavernSeatCard'

function seat(over: Partial<PlayerSeat> = {}): PlayerSeat {
  return {
    schema_version: 1, campaign_id: 'cmp_S1', campaign_name: 'The Hollow Crown', alias: 'Brannoc',
    accepted_at: '2026-09-01T12:00:00Z', confirmed: true, tone: null, game_system: 'dnd5e',
    avatar_icon: 'castle', avatar_tone: 'gold', concluded: false, last_played_at: null, live: false, ...over,
  }
}

const NOW = new Date('2026-10-01T09:00:00Z')
const mount = (over: Partial<PlayerSeat> = {}) => render(<TavernSeatCard seat={seat(over)} now={NOW} />)

afterEach(() => {
  vi.restoreAllMocks()
})

describe('TavernSeatCard (30c PR-2, C-9 to C-14)', () => {
  it('C-9 is a non-interactive group named by the table, with nothing in it to press or follow', async () => {
    mount({ live: true })
    const group = screen.getByRole('group', { name: 'The Hollow Crown' })
    expect(group).not.toHaveAttribute('role', 'button')
    expect(group).not.toHaveAttribute('tabindex')
    expect(group.className).not.toContain('card--interactive')
    expect(within(group).queryAllByRole('button')).toHaveLength(0)
    expect(within(group).queryAllByRole('link')).toHaveLength(0)
    expect(group.querySelector('a')).toBeNull()
    await userEvent.click(group)
    await userEvent.click(screen.getByText('Live now'))
    expect(screen.getByRole('heading', { level: 2, name: 'The Hollow Crown' })).toBeInTheDocument()
  })

  it('C-10 a confirmed seat at a live table reads "Live now" as plain text, and that is the only standing it states (ID-17)', () => {
    mount({ live: true })
    expect(screen.getByText('Live now')).toBeInTheDocument()
    expect(screen.queryByText('Waiting for your GM to confirm your seat')).toBeNull()
    expect(screen.queryByText('This table has concluded')).toBeNull()
    expect(screen.queryByText('Last played', { exact: false })).toBeNull()
  })

  it('C-11 an unconfirmed seat says it is waiting, and never "Live now", even at a live table (D-12)', () => {
    mount({ confirmed: false, live: true, last_played_at: '2026-09-04T12:00:00Z' })
    expect(screen.getByText('Waiting for your GM to confirm your seat')).toBeInTheDocument()
    expect(screen.queryByText('Live now')).toBeNull()
    expect(screen.queryByText('Last played', { exact: false })).toBeNull()
  })

  it('C-12 a concluded table says so, and is never live or waiting', () => {
    mount({ concluded: true, live: true, confirmed: false })
    expect(screen.getByText('This table has concluded')).toBeInTheDocument()
    expect(screen.queryByText('Live now')).toBeNull()
    expect(screen.queryByText('Waiting for your GM to confirm your seat')).toBeNull()
  })

  it('C-13 a quiet confirmed seat names when the table last met, in the year only when it is not this one; never met says nothing', () => {
    const { unmount } = mount({ last_played_at: '2026-09-04T12:00:00Z' })
    expect(screen.getByText('Last played 4 September')).toBeInTheDocument()
    unmount()
    const prior = mount({ last_played_at: '2025-03-04T12:00:00Z' })
    expect(screen.getByText('Last played 4 March 2025')).toBeInTheDocument()
    prior.unmount()
    mount()
    expect(screen.queryByText('Last played', { exact: false })).toBeNull()
  })

  it('C-14 shows the table facts it was given and no more: alias, tone when set, the system, no seat count', () => {
    const { unmount } = mount({ tone: 'Mystery · Low magic' })
    expect(screen.getByText('Playing as Brannoc')).toBeInTheDocument()
    expect(screen.getByText('Mystery · Low magic')).toBeInTheDocument()
    expect(screen.getByText('5e')).toBeInTheDocument()
    expect(screen.queryByText(/\d+ players?/)).toBeNull()
    unmount()
    mount()
    expect(document.querySelector('.tavern-card__tone')).toBeNull()
  })
})
