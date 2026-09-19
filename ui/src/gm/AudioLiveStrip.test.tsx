/**
 * AudioLiveStrip — the workspace-header strip (AUDIO-31).
 *
 * What is asserted: two slots at most (AUDIO-1), Stop all rather than a named
 * Stop (AUDIO-9), a Stop that is never disabled in any state (AUDIO-17, X-3),
 * the presence sentence verbatim (AUDIO-21) and kept out of the live region
 * (AUDIO-22, STATE-7), and Stop-only controls with no way to start audio (X-9).
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AudioLiveStrip } from './AudioLiveStrip'
import type { AudioLiveStripProps } from './AudioLiveStrip'
import { PRESENCE_MIXED, PRESENCE_SETTLED, makeGuests, makeParticipant } from './audioFixtures'

const AMBIENCE = { slot: 'ambience', title: 'Tidewarden Chant', loop: true } as const
const ONE_SHOT = { slot: 'one_shot', title: 'Thunderclap', loop: false } as const

function setup(overrides: Partial<AudioLiveStripProps> = {}) {
  const onStopAll = vi.fn()
  const onStopSlot = vi.fn()
  const props: AudioLiveStripProps = {
    slots: [AMBIENCE],
    presence: PRESENCE_SETTLED,
    onStopAll,
    ...overrides,
  }
  const view = render(<AudioLiveStrip {...props} />)
  return { ...view, onStopAll, onStopSlot }
}

// ── AUDIO-1, AUDIO-31: what is live, in the header ───────────────────────────

describe('the two slots (AUDIO-1)', () => {
  it('names the one live cue and how it is playing', () => {
    setup()

    expect(screen.getByRole('region', { name: 'Live table audio' })).toBeInTheDocument()
    expect(screen.getByText('Tidewarden Chant')).toBeInTheDocument()
    expect(screen.getByText('Ambience · loops')).toBeInTheDocument()
  })

  it('lists both slots when both are sounding', () => {
    setup({ slots: [AMBIENCE, ONE_SHOT] })

    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('Tidewarden Chant')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Thunderclap')).toBeInTheDocument()
    expect(within(rows[1]).getByText('One-shot')).toBeInTheDocument()
  })

  it('says an ambience plays once when its loop is off (AUDIO-3)', () => {
    setup({ slots: [{ slot: 'ambience', title: 'Tidewarden Chant', loop: false }] })
    expect(screen.getByText('Ambience · plays once')).toBeInTheDocument()
  })

  it('is absent when nothing is live on a healthy stream (§12.2)', () => {
    const { container } = setup({ slots: [] })
    expect(container).toBeEmptyDOMElement()
  })

  it('stays on screen while the stream is down, because liveness is then unknown (X-3)', () => {
    setup({ slots: [], connection: 'reconnecting' })

    expect(screen.getByRole('region', { name: 'Live table audio' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop all' })).toBeEnabled()
    expect(screen.queryByRole('list')).toBeNull()
  })
})

// ── AUDIO-9, AUDIO-17, X-3: the Stops ────────────────────────────────────────

describe('Stop all (AUDIO-9)', () => {
  it('belongs to the strip and clears both slots', async () => {
    const user = userEvent.setup()
    const { onStopAll } = setup({ slots: [AMBIENCE, ONE_SHOT] })

    await user.click(screen.getByRole('button', { name: 'Stop all' }))
    expect(onStopAll).toHaveBeenCalledTimes(1)
  })

  it('is never disabled, connected or not (AUDIO-17, X-3)', () => {
    const { rerender } = setup()
    expect(screen.getByRole('button', { name: 'Stop all' })).toBeEnabled()

    rerender(
      <AudioLiveStrip slots={[AMBIENCE]} presence={PRESENCE_SETTLED} connection="reconnecting" onStopAll={vi.fn()} />,
    )
    expect(screen.getByRole('button', { name: 'Stop all' })).toBeEnabled()
  })

  it('says Reconnecting… while the stream is down', () => {
    setup({ connection: 'reconnecting' })
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting…')
  })

  it('says nothing in its live region while the stream is healthy', () => {
    setup()
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('offers a Stop that names one cue only when the owner wants the named list (§10.2)', async () => {
    const user = userEvent.setup()
    const onStopSlot = vi.fn()
    render(
      <AudioLiveStrip
        slots={[AMBIENCE, ONE_SHOT]}
        presence={PRESENCE_SETTLED}
        onStopAll={vi.fn()}
        onStopSlot={onStopSlot}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Stop Thunderclap' }))
    expect(onStopSlot).toHaveBeenCalledWith('one_shot')
  })

  it('shows no per-slot Stop by default', () => {
    setup({ slots: [AMBIENCE, ONE_SHOT] })

    expect(screen.queryByRole('button', { name: 'Stop Tidewarden Chant' })).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })
})

// ── X-9: Stop-only. Nothing here starts audio ────────────────────────────────

describe('Stop-only (X-9, AUDIO-31)', () => {
  it('offers no play, push, seek or volume control', () => {
    setup({ slots: [AMBIENCE, ONE_SHOT], onStopSlot: vi.fn() })

    expect(screen.queryByRole('slider')).toBeNull()
    // Every control on the strip is a Stop, judged by accessible name.
    expect(screen.getAllByRole('button', { name: /^Stop/ })).toHaveLength(
      screen.getAllByRole('button').length,
    )
  })

  it('keeps the pulse decorative', () => {
    const { container } = setup()
    expect(container.querySelector('.gm-audio-strip__pulse')).toHaveAttribute('aria-hidden', 'true')
  })

  it('has no canvas prop at all (LIB-14)', () => {
    // @ts-expect-error — audio never opens the canvas, so this callback does
    // not exist on the strip either.
    const absent: keyof AudioLiveStripProps = 'onOpenCanvas'
    expect(absent).toBe('onOpenCanvas')
  })
})

// ── AUDIO-21, AUDIO-22: presence ─────────────────────────────────────────────

describe('presence (AUDIO-21, AUDIO-22)', () => {
  it('writes the sentence, naming participants and counting guests', () => {
    setup({ presence: PRESENCE_MIXED })
    expect(
      screen.getByText("2 listening · 1 muted · Gorath and 1 guest haven't tapped in yet"),
    ).toBeInTheDocument()
  })

  it('never names a guest', () => {
    setup({
      presence: { participants: [makeParticipant('Ondrey', 'listening')], guests: makeGuests({ connected: 2, pending: 2 }) },
    })

    const sentence = screen.getByText(/listening/).textContent ?? ''
    expect(sentence).toBe("1 listening · 0 muted · 2 guests haven't tapped in yet")
    expect(sentence).not.toMatch(/guest[a-z]*\s+[A-Z]/)
  })

  it('keeps the counts OUT of the live region, so they never announce on a change', () => {
    const { rerender } = setup({ presence: PRESENCE_SETTLED })
    const status = screen.getByRole('status')
    const sentence = screen.getByText(/listening/)

    expect(status).not.toContainElement(sentence)
    expect(status).toHaveTextContent('')

    rerender(<AudioLiveStrip slots={[AMBIENCE]} presence={PRESENCE_MIXED} onStopAll={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('has exactly one polite live region', () => {
    setup({ slots: [AMBIENCE, ONE_SHOT], presence: PRESENCE_MIXED, connection: 'reconnecting' })
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })
})
