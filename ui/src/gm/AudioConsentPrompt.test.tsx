/**
 * AudioConsentPrompt — the TABLE client's sound gate.
 *
 * The two rules that matter most here are privacy and storage: a table client
 * is never given a cue's title or filename (AUDIO-29), and consent is one tap
 * per page load held in memory only, so this component writes nothing at all
 * (AUDIO-18, X-4, X-7). Mute is a player toggle and stays available even while
 * the browser cannot play the sound (AUDIO-20, §12.2).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AudioConsentPrompt } from './AudioConsentPrompt'
import type { AudioConsentPromptProps } from './AudioConsentPrompt'
import { AMBIENCE_CUE } from './audioFixtures'

function setup(overrides: Partial<AudioConsentPromptProps> = {}) {
  const onAccept = vi.fn()
  const onMuteChange = vi.fn()
  const props: AudioConsentPromptProps = {
    accepted: false,
    muted: false,
    onAccept,
    onMuteChange,
    ...overrides,
  }
  const view = render(<AudioConsentPrompt {...props} />)
  return { ...view, onAccept, onMuteChange }
}

// ── AUDIO-19: the banner, then the full prompt ───────────────────────────────

describe('the tap target (AUDIO-19)', () => {
  it('shows the small banner as soon as a client joins', () => {
    setup()

    expect(screen.getByRole('button', { name: 'Tap to enable sound' })).toBeInTheDocument()
    expect(screen.queryByText('Your GM is playing something')).toBeNull()
  })

  it('shows the full prompt when a cue arrives before the tap', () => {
    setup({ cueWaiting: true })

    expect(screen.getByRole('button', { name: /Tap to enable sound/ })).toBeInTheDocument()
    expect(screen.getByText('Your GM is playing something')).toBeInTheDocument()
  })

  it('accepts on one tap', async () => {
    const user = userEvent.setup()
    const { onAccept } = setup({ cueWaiting: true })

    await user.click(screen.getByRole('button', { name: /Tap to enable sound/ }))
    expect(onAccept).toHaveBeenCalledTimes(1)
  })

  it('replaces the tap target with mute once accepted', () => {
    setup({ accepted: true })

    expect(screen.queryByRole('button', { name: /Tap to enable sound/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Mute' })).toBeInTheDocument()
  })

  it('is reachable from the keyboard', async () => {
    const user = userEvent.setup()
    const { onAccept } = setup()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Tap to enable sound' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onAccept).toHaveBeenCalledTimes(1)
  })
})

// ── AUDIO-20: mute ───────────────────────────────────────────────────────────

describe('mute (AUDIO-20)', () => {
  it('is a pressed toggle, off then on', async () => {
    const user = userEvent.setup()
    const { onMuteChange, rerender } = setup({ accepted: true })

    const mute = screen.getByRole('button', { name: 'Mute' })
    expect(mute).toHaveAttribute('aria-pressed', 'false')

    await user.click(mute)
    expect(onMuteChange).toHaveBeenCalledWith(true)

    rerender(<AudioConsentPrompt accepted muted onAccept={vi.fn()} onMuteChange={onMuteChange} />)
    expect(screen.getByRole('button', { name: 'Mute' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('turns back off from the pressed state', async () => {
    const user = userEvent.setup()
    const { onMuteChange } = setup({ accepted: true, muted: true })

    await user.click(screen.getByRole('button', { name: 'Mute' }))
    expect(onMuteChange).toHaveBeenCalledWith(false)
  })

  it('stays available while the browser cannot play the sound (§12.2)', () => {
    setup({ accepted: true, status: 'error' })

    expect(screen.getByRole('button', { name: 'Mute' })).toBeEnabled()
    expect(screen.getByRole('status')).toHaveTextContent("Your browser can't play this sound")
  })

  it('is the one control a player gets — there is no transport here', () => {
    setup({ accepted: true })

    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.queryByRole('slider')).toBeNull()
  })
})

// ── §12.2, audio (player): one polite region ─────────────────────────────────

describe('status (STATE-7, §12.2)', () => {
  it('has exactly one polite live region, empty at rest', () => {
    setup({ accepted: true })

    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('reports loading in the words the record gives', () => {
    setup({ accepted: true, status: 'loading' })
    expect(screen.getByRole('status')).toHaveTextContent('Loading sound…')
  })
})

// ── AUDIO-29, AUDIO-18: what a player is never given, and never keeps ────────

describe('privacy and the consent boundary (AUDIO-29, AUDIO-18)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('has no prop that could carry a cue title or filename', () => {
    // @ts-expect-error — AUDIO-29: table clients never receive a cue's title.
    const absentTitle: keyof AudioConsentPromptProps = 'title'
    // @ts-expect-error — AUDIO-29: nor its filename.
    const absentFile: keyof AudioConsentPromptProps = 'filename'
    expect([absentTitle, absentFile]).toEqual(['title', 'filename'])
  })

  it('renders no cue title anywhere, in any state', () => {
    const { container, rerender } = setup({ cueWaiting: true })
    expect(container.textContent ?? '').not.toContain(AMBIENCE_CUE.title)

    rerender(<AudioConsentPrompt accepted muted status="error" onAccept={vi.fn()} onMuteChange={vi.fn()} />)
    expect(container.textContent ?? '').not.toContain(AMBIENCE_CUE.title)
    for (const element of container.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        expect(attribute.value).not.toContain(AMBIENCE_CUE.title)
      }
    }
  })

  it('writes nothing to web storage — consent lives in memory for one page load', async () => {
    const user = userEvent.setup()
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const { rerender, onMuteChange } = setup({ cueWaiting: true })

    await user.click(screen.getByRole('button', { name: /Tap to enable sound/ }))
    rerender(<AudioConsentPrompt accepted muted={false} onAccept={vi.fn()} onMuteChange={onMuteChange} />)
    await user.click(screen.getByRole('button', { name: 'Mute' }))

    expect(setItem).not.toHaveBeenCalled()
  })

  it('asks again after a reload, because acceptance is a prop and not a memory', () => {
    const { unmount } = setup({ accepted: true })
    unmount()

    // A fresh page load hands `accepted: false` back in; nothing was persisted
    // that could short-circuit the tap (AUDIO-18).
    setup({ accepted: false })
    expect(screen.getByRole('button', { name: 'Tap to enable sound' })).toBeInTheDocument()
  })
})
