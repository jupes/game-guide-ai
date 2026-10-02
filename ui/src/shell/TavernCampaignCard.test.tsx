/**
 * TavernCampaignCard.test.tsx -- one owned campaign card in its three variants
 * (agent-forge-harness-30c, PR-1, C-1 to C-8).
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Campaign } from '../gm/contracts'
import { TavernCampaignCard, type TavernCardVariant } from './TavernCampaignCard'

function campaign(over: Partial<Campaign> = {}): Campaign {
  return {
    schema_version: 1, campaign_id: 'cmp_A', name: 'The Hollow Crown', created_at: '2026-09-01T12:00:00Z',
    updated_at: '2026-09-01T12:00:00Z', archived_at: null, concluded_at: null, tone: null,
    game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-07-04T12:00:00Z', last_played_at: null, dormant: false, ...over,
  }
}

function mount(variant: TavernCardVariant, over: Partial<Campaign> = {}, busy = false) {
  const handlers = { onPrep: vi.fn(), onConclude: vi.fn(), onReopen: vi.fn() }
  const landing = React.createRef<HTMLElement>()
  const view = render(
    <TavernCampaignCard
      campaign={campaign(over)}
      variant={variant}
      busy={busy}
      landing={landing}
      now={new Date('2026-10-01T09:00:00Z')}
      {...handlers}
    />,
  )
  return { ...handlers, view }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('TavernCampaignCard (30c, C-1 to C-8)', () => {
  it('C-1 is a non-interactive group named by the campaign: not a button, and a click on it does nothing', async () => {
    const m = mount('active')
    const group = screen.getByRole('group', { name: 'The Hollow Crown' })
    expect(group).not.toHaveAttribute('role', 'button')
    expect(group).not.toHaveAttribute('tabindex')
    expect(group.className).not.toContain('card--interactive')
    expect(group.querySelector('.card__state-layer')).toBeNull()
    await userEvent.click(screen.getByRole('heading', { level: 2, name: 'The Hollow Crown' }))
    await userEvent.click(group)
    expect(m.onPrep).not.toHaveBeenCalled()
    expect(m.onConclude).not.toHaveBeenCalled()
    expect(m.onReopen).not.toHaveBeenCalled()
  })

  it('C-2 Prep is named "Prep {name}" and calls onPrep', async () => {
    const m = mount('active')
    await userEvent.click(screen.getByRole('button', { name: 'Prep The Hollow Crown' }))
    expect(m.onPrep).toHaveBeenCalledTimes(1)
  })

  it('C-3 Start Session is aria-disabled, described by the Loremaster line, and pressing it calls nothing', async () => {
    const m = mount('active')
    const start = screen.getByRole('button', { name: 'Start Session The Hollow Crown' })
    expect(start).toHaveAttribute('aria-disabled', 'true')
    expect(start).not.toBeDisabled()
    expect(start).toHaveAccessibleDescription('Running a live table is part of Loremaster')
    await userEvent.click(start)
    start.focus()
    await userEvent.keyboard('{Enter}')
    await userEvent.keyboard(' ')
    expect(m.onPrep).not.toHaveBeenCalled()
    expect(m.onConclude).not.toHaveBeenCalled()
    expect(m.onReopen).not.toHaveBeenCalled()
  })

  it('C-4 dormant reads "Dormant · no activity since …" with Mark concluded, and has no Start Session', async () => {
    const m = mount('dormant', { dormant: true })
    expect(screen.getByText('Dormant · no activity since 4 July')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Start Session/ })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Mark concluded The Hollow Crown' }))
    expect(m.onConclude).toHaveBeenCalledTimes(1)
    expect(m.onReopen).not.toHaveBeenCalled()
    expect(screen.getByRole('group').className).toContain('tavern-card--dormant')
  })

  it('C-4 the variant, not the dormant flag, decides what renders; and a year in the past is named', () => {
    mount('active', { dormant: true })
    expect(screen.queryByText(/Dormant/)).toBeNull()
    expect(screen.queryByRole('button', { name: /^Mark concluded/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Start Session The Hollow Crown' })).toBeInTheDocument()
  })

  it('C-4 an earlier year is part of the date', () => {
    mount('dormant', { dormant: true, last_activity_at: '2025-03-04T12:00:00Z' })
    expect(screen.getByText('Dormant · no activity since 4 March 2025')).toBeInTheDocument()
  })

  it('C-5 an active card has no Mark concluded and no Reopen', () => {
    mount('active')
    expect(screen.queryByRole('button', { name: /^Mark concluded/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Reopen/ })).toBeNull()
    expect(screen.queryByText(/Dormant/)).toBeNull()
  })

  it('C-6 a concluded card offers Reopen and Prep, and no Start Session', async () => {
    const m = mount('concluded', { concluded_at: '2026-09-20T10:00:00Z' })
    expect(screen.queryByRole('button', { name: /^Start Session/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Mark concluded/ })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Reopen The Hollow Crown' }))
    expect(m.onReopen).toHaveBeenCalledTimes(1)
    expect(m.onConclude).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Prep The Hollow Crown' })).toBeInTheDocument()
  })

  it('C-7 the LIVE and READY badges render, and none renders when badge is null', () => {
    const live = mount('active', { badge: 'live' })
    expect(screen.getByText('LIVE')).toHaveClass('badge--nat20')
    live.view.unmount()
    const ready = mount('active', { badge: 'ready' })
    expect(screen.getByText('READY')).toHaveClass('badge--verdigris')
    ready.view.unmount()
    mount('active', { badge: null })
    expect(screen.queryByText('LIVE')).toBeNull()
    expect(screen.queryByText('READY')).toBeNull()
  })

  it('C-8 there is no tone line when tone is null and no seat chip at 0; both show when set', () => {
    const bare = mount('active', { tone: null, seat_count: 0 })
    expect(document.querySelector('.tavern-card__tone')).toBeNull()
    expect(screen.getByText('5e')).toBeInTheDocument()
    expect(screen.queryByText(/player/)).toBeNull()
    expect(document.querySelectorAll('.chip')).toHaveLength(1)
    bare.view.unmount()
    mount('active', { tone: 'Grim and gothic', seat_count: 4 })
    expect(screen.getByText('Grim and gothic')).toBeInTheDocument()
    expect(screen.getByText('4 players')).toBeInTheDocument()
    expect(document.querySelectorAll('.chip')).toHaveLength(2)
  })

  it('the chips are not interactive, and the avatar and every icon are hidden from assistive technology', () => {
    mount('active', { seat_count: 1 })
    expect(screen.getByText('1 player').closest('.chip')).not.toHaveAttribute('role', 'button')
    expect(document.querySelector('.avatar')).toHaveAttribute('aria-hidden', 'true')
    const icons = document.querySelectorAll('.material-symbols-rounded')
    expect(icons.length).toBeGreaterThan(3)
    for (const icon of icons) expect(icon.closest('[aria-hidden="true"]')).not.toBeNull()
  })

  it('a busy pending action is aria-disabled and a press does nothing', async () => {
    const m = mount('dormant', { dormant: true }, true)
    const pending = screen.getByRole('button', { name: 'Mark concluded The Hollow Crown' })
    expect(pending).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(pending)
    expect(m.onConclude).not.toHaveBeenCalled()
  })
})
