/**
 * AudioCue — the GM's cue card.
 *
 * Every acceptance criterion of `1kg.8.4` that concerns the card is asserted
 * here: the private preview is structurally separate from the table's two
 * controls (AUDIO-5 to AUDIO-8), a one-shot has no loop control at all
 * (AUDIO-3), the seek is a labelled slider with AUDIO-10's keyboard geometry,
 * Stop names its cue (AUDIO-9), Stop survives a dropped stream (AUDIO-17), the
 * surface announces once and never per tick (STATE-7), gain goes through the
 * Web Audio stage (AUDIO-30), and nothing here can open the canvas (LIB-14).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AudioCue } from './AudioCue'
import type { AudioCueProps } from './AudioCue'
import { AMBIENCE_CUE, ONE_SHOT_CUE, makeCue } from './audioFixtures'

function callbacks() {
  return {
    onPreviewToggle: vi.fn(),
    onPreviewSeek: vi.fn(),
    onPreviewVolume: vi.fn(),
    onPreviewMute: vi.fn(),
    onLoopChange: vi.fn(),
    onPlayToTable: vi.fn(),
    onStop: vi.fn(),
    onRetryPush: vi.fn(),
    onReplace: vi.fn(),
  }
}

type Spies = ReturnType<typeof callbacks>

function setup(overrides: Partial<AudioCueProps> = {}) {
  const spies = callbacks()
  const props: AudioCueProps = {
    cue: AMBIENCE_CUE,
    preview: { playing: false, positionMs: 42_000, volume: 0.66, muted: false },
    ...spies,
    ...overrides,
  }
  const view = render(<AudioCue {...props} />)
  return { ...view, spies, props }
}

const previewGroup = () => screen.getByRole('group', { name: 'Preview (only you hear this)' })
const tableGroup = () => screen.getByRole('group', { name: 'Table controls' })
const seekSlider = () => screen.getByRole('slider', { name: `Seek ${AMBIENCE_CUE.title}` })

function assertUntouched(spies: Spies, keys: readonly (keyof Spies)[]) {
  for (const key of keys) expect(spies[key], `${key} should not have fired`).not.toHaveBeenCalled()
}

// ── AC 1: preview and table are separate, structurally and behaviourally ─────

describe('preview versus table (AUDIO-5 to AUDIO-8)', () => {
  it('puts the transport in the preview group and nothing table-facing in it', () => {
    setup()
    const preview = within(previewGroup())

    expect(preview.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` })).toBeInTheDocument()
    expect(preview.getByRole('slider', { name: `Seek ${AMBIENCE_CUE.title}` })).toBeInTheDocument()
    expect(preview.queryByRole('button', { name: 'Play to table' })).toBeNull()
    expect(preview.queryByRole('button', { name: /^Stop/ })).toBeNull()
  })

  it('gives the table exactly Play to table and Stop — no seek, no pause (AUDIO-8)', () => {
    setup({ live: true })
    const table = within(tableGroup())

    expect(table.getAllByRole('button')).toHaveLength(2)
    expect(table.getByRole('button', { name: 'Play to table' })).toBeInTheDocument()
    expect(table.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })).toBeInTheDocument()
    expect(table.queryByRole('slider')).toBeNull()
    expect(table.queryByRole('button', { name: /pause/i })).toBeNull()
  })

  it('emits nothing to the table from any preview interaction (AUDIO-7)', async () => {
    const user = userEvent.setup()
    const { spies } = setup()

    await user.click(screen.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` }))
    fireEvent.change(seekSlider(), { target: { value: '90000' } })
    fireEvent.keyDown(seekSlider(), { key: 'ArrowRight' })
    fireEvent.change(screen.getByRole('slider', { name: 'Preview volume' }), { target: { value: '0.2' } })
    await user.click(screen.getByRole('button', { name: `Mute preview of ${AMBIENCE_CUE.title}` }))

    expect(spies.onPreviewToggle).toHaveBeenCalledTimes(1)
    expect(spies.onPreviewSeek).toHaveBeenCalledTimes(2)
    expect(spies.onPreviewVolume).toHaveBeenCalledWith(0.2)
    expect(spies.onPreviewMute).toHaveBeenCalledWith(true)
    assertUntouched(spies, ['onPlayToTable', 'onStop', 'onRetryPush', 'onReplace'])
  })

  it('emits nothing private from the table controls', async () => {
    const user = userEvent.setup()
    const { spies } = setup({ live: true })

    await user.click(screen.getByRole('button', { name: 'Play to table' }))
    await user.click(screen.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` }))

    expect(spies.onPlayToTable).toHaveBeenCalledTimes(1)
    expect(spies.onStop).toHaveBeenCalledTimes(1)
    assertUntouched(spies, ['onPreviewToggle', 'onPreviewSeek', 'onPreviewVolume', 'onPreviewMute'])
  })

  it('offers the GM their own volume and a local mute (AUDIO-5, AUDIO-6)', () => {
    setup({ preview: { playing: true, positionMs: 0, volume: 0.25, muted: true } })

    const volume = screen.getByRole('slider', { name: 'Preview volume' })
    expect(volume).toHaveValue('0.25')
    expect(volume).toHaveAttribute('aria-valuetext', '25%')
    expect(screen.getByRole('button', { name: `Mute preview of ${AMBIENCE_CUE.title}` })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('says what the table still hears while another cue is previewed (AUDIO-6)', () => {
    setup({ tableHearing: 'Storm Over Saltmarsh' })
    expect(screen.getByText('Table still hearing: Storm Over Saltmarsh')).toBeInTheDocument()
  })

  it('names the transport for pause when the preview is running', () => {
    setup({ preview: { playing: true, positionMs: 0, volume: 0.5, muted: false } })
    expect(
      screen.getByRole('button', { name: `Pause preview of ${AMBIENCE_CUE.title}` }),
    ).toBeInTheDocument()
  })
})

// ── AC 2: a one-shot cannot be accidentally looped ───────────────────────────

describe('kind (AUDIO-3, AUDIO-4)', () => {
  it('renders a loop toggle for ambience', () => {
    setup({ loop: true })
    const loop = screen.getByRole('button', { name: `Loop ${AMBIENCE_CUE.title}` })
    expect(loop).toHaveAttribute('aria-pressed', 'true')
  })

  it('does not render the loop control for a one-shot at all — not even disabled', () => {
    render(
      <AudioCue
        cue={ONE_SHOT_CUE}
        preview={{ playing: false, positionMs: 0, volume: 0.5, muted: false }}
        {...callbacks()}
      />,
    )

    expect(screen.queryByRole('button', { name: /loop/i })).toBeNull()
    expect(screen.queryByText(/loop/i)).toBeNull()
    // Nothing disabled is hiding there either.
    for (const button of screen.getAllByRole('button')) {
      expect(button.textContent?.toLowerCase() ?? '').not.toContain('loop')
    }
  })

  it('reports the loop flag in the kind line, and never offers to change kind', () => {
    const { rerender } = setup({ loop: true })
    expect(screen.getByText('Ambience · loops')).toBeInTheDocument()

    rerender(
      <AudioCue
        cue={AMBIENCE_CUE}
        loop={false}
        preview={{ playing: false, positionMs: 0, volume: 0.5, muted: false }}
        {...callbacks()}
      />,
    )
    expect(screen.getByText('Ambience · plays once')).toBeInTheDocument()

    // AUDIO-4: kind is immutable, so no control anywhere names it.
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.queryByRole('radiogroup')).toBeNull()
    expect(screen.queryByRole('button', { name: /ambience|one-shot|kind/i })).toBeNull()
  })

  it('reports a one-shot kind without a loop claim', () => {
    render(
      <AudioCue
        cue={ONE_SHOT_CUE}
        preview={{ playing: false, positionMs: 0, volume: 0.5, muted: false }}
        {...callbacks()}
      />,
    )
    expect(screen.getByText('One-shot')).toBeInTheDocument()
  })

  it('toggles loop without touching the table', async () => {
    const user = userEvent.setup()
    const { spies } = setup({ loop: true })

    await user.click(screen.getByRole('button', { name: `Loop ${AMBIENCE_CUE.title}` }))

    expect(spies.onLoopChange).toHaveBeenCalledWith(false)
    assertUntouched(spies, ['onPlayToTable', 'onStop'])
  })
})

// ── AC 3: the waveform is operable without a pointer ─────────────────────────

describe('seek slider (AUDIO-10)', () => {
  it('is a labelled slider that says where it is in words', () => {
    setup()
    const slider = seekSlider()

    expect(slider).toHaveAttribute('aria-valuetext', '0:42 of 3:10')
    expect(slider).toHaveAttribute('max', String(AMBIENCE_CUE.duration_ms))
    expect(slider).toHaveValue('42000')
  })

  it('moves 5 seconds on the arrows and swallows the key', () => {
    const { spies } = setup()

    expect(fireEvent.keyDown(seekSlider(), { key: 'ArrowRight' })).toBe(false)
    expect(spies.onPreviewSeek).toHaveBeenLastCalledWith(47_000)

    fireEvent.keyDown(seekSlider(), { key: 'ArrowLeft' })
    expect(spies.onPreviewSeek).toHaveBeenLastCalledWith(37_000)
  })

  it('moves 30 seconds on the Page keys', () => {
    const { spies } = setup()

    fireEvent.keyDown(seekSlider(), { key: 'PageUp' })
    expect(spies.onPreviewSeek).toHaveBeenLastCalledWith(72_000)

    fireEvent.keyDown(seekSlider(), { key: 'PageDown' })
    expect(spies.onPreviewSeek).toHaveBeenLastCalledWith(12_000)
  })

  it('jumps to both ends on Home and End', () => {
    const { spies } = setup()

    fireEvent.keyDown(seekSlider(), { key: 'Home' })
    expect(spies.onPreviewSeek).toHaveBeenLastCalledWith(0)

    fireEvent.keyDown(seekSlider(), { key: 'End' })
    expect(spies.onPreviewSeek).toHaveBeenLastCalledWith(AMBIENCE_CUE.duration_ms)
  })

  it('leaves keys it does not own alone, so Tab still escapes the slider', () => {
    const { spies } = setup()

    expect(fireEvent.keyDown(seekSlider(), { key: 'Tab' })).toBe(true)
    expect(fireEvent.keyDown(seekSlider(), { key: 'ArrowRight', ctrlKey: true })).toBe(true)
    expect(spies.onPreviewSeek).not.toHaveBeenCalled()
  })

  it('is reachable by keyboard and shows focus', async () => {
    const user = userEvent.setup()
    setup()

    await user.tab()
    await user.tab()
    expect(seekSlider()).toHaveFocus()
  })

  it('keeps the bars decorative', () => {
    const { container } = setup()
    const bars = container.querySelector('.gm-audio-cue__bars')

    expect(bars).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getAllByRole('slider')).toHaveLength(2)
  })

  it('draws the peaks it is given, and a deterministic placeholder otherwise', () => {
    const { container, rerender } = setup({ peaks: [0.2, 0.9, 0.5] })
    expect(container.querySelectorAll('.gm-audio-cue__bar')).toHaveLength(3)
    expect(container.querySelectorAll('.gm-audio-cue__bar--played')).toHaveLength(1)

    rerender(
      <AudioCue
        cue={AMBIENCE_CUE}
        preview={{ playing: false, positionMs: 0, volume: 0.5, muted: false }}
        {...callbacks()}
      />,
    )
    expect(container.querySelectorAll('.gm-audio-cue__bar').length).toBeGreaterThan(3)
  })

  it('does not divide by a zero duration', () => {
    render(
      <AudioCue
        cue={makeCue({ duration_ms: 0 })}
        preview={{ playing: false, positionMs: 0, volume: 0.5, muted: false }}
        {...callbacks()}
      />,
    )

    const slider = screen.getByRole('slider', { name: `Seek ${AMBIENCE_CUE.title}` })
    expect(slider).toHaveAttribute('aria-valuetext', '0:00 of 0:00')
  })
})

// ── AC 4: announcements are rationed ─────────────────────────────────────────

describe('announcements (STATE-7)', () => {
  it('has exactly one polite live region', () => {
    setup({ live: true })
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('says nothing at rest', () => {
    setup()
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('does not change on a playback tick, a seek step or a volume change', () => {
    const preview = { playing: true, positionMs: 0, volume: 0.5, muted: false }
    const { rerender } = setup({ live: true, preview })
    const before = screen.getByRole('status').textContent

    for (const next of [1_000, 2_000, 3_000, 90_000]) {
      rerender(
        <AudioCue
          cue={AMBIENCE_CUE}
          live
          preview={{ ...preview, positionMs: next, volume: next / 200_000 }}
          {...callbacks()}
        />,
      )
      expect(screen.getByRole('status').textContent).toBe(before)
    }
    expect(before).toBe('Playing to the table')
  })

  it('announces the states the record names', () => {
    const { rerender } = setup({ playability: 'processing' })
    expect(screen.getByRole('status')).toHaveTextContent('Still processing…')

    const base = {
      cue: AMBIENCE_CUE,
      preview: { playing: false, positionMs: 0, volume: 0.5, muted: false },
      ...callbacks(),
    }
    rerender(<AudioCue {...base} playability="failed" />)
    expect(screen.getByRole('status')).toHaveTextContent("This file couldn't be processed")

    rerender(<AudioCue {...base} pushFailed />)
    expect(screen.getByRole('status')).toHaveTextContent("Couldn't play to the table")

    rerender(<AudioCue {...base} preview={{ ...base.preview, blocked: true }} />)
    expect(screen.getByRole('status')).toHaveTextContent('Press play again to allow sound')
  })
})

// ── AC 6: audio never opens the canvas ───────────────────────────────────────

describe('never opens the canvas (LIB-14)', () => {
  it('has no canvas prop at all', () => {
    // @ts-expect-error — LIB-14: activating a cue expands its card, it never
    // opens the canvas, so this component has no such callback to call.
    const absent: keyof AudioCueProps = 'onOpenCanvas'
    expect(absent).toBe('onOpenCanvas')
  })

  it('renders no control that would take the GM to a document', () => {
    setup({ live: true })

    expect(screen.queryByRole('link')).toBeNull()
    for (const button of screen.getAllByRole('button')) {
      expect(button.textContent ?? '').not.toMatch(/canvas|open in/i)
    }
  })
})

// ── AUDIO-9 and AUDIO-17: the two Stops, and a dropped stream ────────────────

describe('Stop and reconnection (AUDIO-9, AUDIO-17)', () => {
  it('names the cue this Stop would clear', () => {
    setup({ live: true })
    const stop = screen.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })

    expect(stop).toBeEnabled()
    expect(stop).toHaveTextContent('Stop')
  })

  it('disables Play to table but not Stop while the stream is down', () => {
    setup({ live: true, connection: 'reconnecting' })

    expect(screen.getByRole('button', { name: 'Play to table' })).toBeDisabled()
    expect(screen.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })).toBeEnabled()
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting…')
  })

  it('keeps Stop enabled while disconnected even when this cue was not live', () => {
    setup({ live: false, connection: 'reconnecting' })
    expect(screen.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })).toBeEnabled()
  })

  it('has nothing to stop on a healthy stream when the cue holds no slot', () => {
    setup({ live: false })
    expect(screen.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })).toBeDisabled()
  })
})

// ── §12.2: the cue card's loading, error and retry rows ──────────────────────

describe('loading, error and retry (§12.2)', () => {
  it('disables preview and push until the bytes are playable', () => {
    setup({ playability: 'processing' })

    expect(screen.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` })).toBeDisabled()
    expect(seekSlider()).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Play to table' })).toBeDisabled()
  })

  it('offers Replace for a file that could not be processed', async () => {
    const user = userEvent.setup()
    const { spies } = setup({ playability: 'failed' })

    await user.click(screen.getByRole('button', { name: 'Replace' }))
    expect(spies.onReplace).toHaveBeenCalledTimes(1)
  })

  it('offers Retry inline beside a failed push', async () => {
    const user = userEvent.setup()
    const { spies } = setup({ pushFailed: true })

    const retry = within(tableGroup()).getByRole('button', { name: 'Retry' })
    await user.click(retry)
    expect(spies.onRetryPush).toHaveBeenCalledTimes(1)
  })

  it('shows neither recovery action when nothing has failed', () => {
    setup()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Replace' })).toBeNull()
  })
})

// ── AUDIO-30: gain goes through Web Audio, and only from a gesture ───────────

describe('the preview gain stage (AUDIO-30)', () => {
  /** Shaped to `PreviewGainStage`, so a contract change breaks the call sites. */
  function fakeStage() {
    return {
      started: false,
      unavailable: false,
      unlock: vi.fn<() => void>(),
      setGain: vi.fn<(value: number) => void>(),
      dispose: vi.fn<() => void>(),
    }
  }

  it('unlocks the context from the play gesture and nowhere else', async () => {
    const user = userEvent.setup()
    const stage = fakeStage()
    setup({ previewGain: stage })

    expect(stage.unlock).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` }))
    expect(stage.unlock).toHaveBeenCalledTimes(1)
  })

  it('routes the GM volume and the local mute through the stage', () => {
    const stage = fakeStage()
    const props = {
      cue: AMBIENCE_CUE,
      previewGain: stage,
      ...callbacks(),
    }
    const { rerender } = render(
      <AudioCue {...props} preview={{ playing: false, positionMs: 0, volume: 0.4, muted: false }} />,
    )
    expect(stage.setGain).toHaveBeenLastCalledWith(0.4)

    rerender(<AudioCue {...props} preview={{ playing: false, positionMs: 0, volume: 0.4, muted: true }} />)
    expect(stage.setGain).toHaveBeenLastCalledWith(0)
  })

  it('renders without a stage at all', async () => {
    const user = userEvent.setup()
    const { spies } = setup({ previewGain: null })

    await user.click(screen.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` }))
    expect(spies.onPreviewToggle).toHaveBeenCalledTimes(1)
  })
})

// ── X-7: the card writes no storage ──────────────────────────────────────────

describe('privacy (X-7, AUDIO-29)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('never writes a cue title into web storage', async () => {
    const user = userEvent.setup()
    const local = vi.spyOn(Storage.prototype, 'setItem')
    const { spies } = setup({ live: true })

    await user.click(screen.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` }))
    await user.click(screen.getByRole('button', { name: 'Play to table' }))

    expect(local).not.toHaveBeenCalled()
    expect(spies.onPlayToTable).toHaveBeenCalled()
  })

  it('keeps the title out of every id, class and data attribute', () => {
    const { container } = setup({ live: true })
    const needle = AMBIENCE_CUE.title.toLowerCase()

    for (const element of container.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        if (attribute.name === 'aria-label' || attribute.name === 'aria-valuetext') continue
        expect(
          attribute.value.toLowerCase(),
          `${attribute.name} must not carry the cue title`,
        ).not.toContain(needle)
      }
    }
  })
})
