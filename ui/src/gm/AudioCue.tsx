/**
 * AudioCue — the GM's cue card (`1kg.8.4`).
 *
 * The load-bearing shape of this component is the split between two groups of
 * controls, and it is structural, not decorative:
 *
 *  - PREVIEW (AUDIO-5, AUDIO-6, AUDIO-7) — play/pause, a keyboard-seekable
 *    slider, the GM's own volume and a local mute. None of it reaches the
 *    table: these controls call preview callbacks and nothing else.
 *  - TABLE (AUDIO-8) — exactly **Play to table** and **Stop**. A push always
 *    starts at zero, so there is no table seek and no table pause.
 *
 * Other rules this renders: a one-shot has no loop control at all (AUDIO-3);
 * kind is immutable, so nothing here can change it (AUDIO-4); Stop names its
 * cue (AUDIO-9); the seek is a labelled slider with arrow/Page/Home/End
 * geometry (AUDIO-10); Play to table is disabled while disconnected but Stop is
 * not (AUDIO-17); every gain change goes through the injected Web Audio stage
 * (AUDIO-30); and audio never opens the canvas, so no such callback exists
 * (LIB-14).
 *
 * It is fully controlled: it owns no playback, no network and no storage.
 */

import * as React from 'react'
import './AudioCue.css'
import { Badge } from '../ds/Badge'
import { Button } from '../ds/Button'
import type { Cue } from './contracts'
import type { AudioConnection, CuePlayability } from './audioHelpers'
import {
  canPlayToTable,
  clampPositionMs,
  clampUnit,
  cueStatusMessage,
  fallbackPeaks,
  formatClock,
  formatRemaining,
  isLoopable,
  kindSummary,
  nextSeekPositionMs,
  seekValueText,
  volumeValueText,
} from './audioHelpers'
import type { PreviewGainStage } from './audioGain'

const BAR_COUNT = 46
/** Pointer drags land on whole seconds; the keyboard uses AUDIO-10's steps. */
const SEEK_STEP_MS = 1000

/** The GM's private transport, as the owner holds it. */
export interface AudioCuePreview {
  playing: boolean
  positionMs: number
  /** 0–1, the GM's own volume (AUDIO-5). */
  volume: number
  /** The local monitor mute (AUDIO-6). */
  muted: boolean
  /** The browser refused to start sound without a gesture (§12.2). */
  blocked?: boolean
}

export interface AudioCueProps {
  cue: Cue
  preview: AudioCuePreview
  /** AUDIO-3. Ignored for a one-shot, which never renders a loop control. */
  loop?: boolean
  /** True while THIS cue holds its slot (AUDIO-9). */
  live?: boolean
  /** AUDIO-6: `Table still hearing: <title>` while the GM previews another cue. */
  tableHearing?: string | null
  /** §12.2: preview and push are disabled until the bytes are playable. */
  playability?: CuePlayability
  /** AUDIO-17. */
  connection?: AudioConnection
  /** §12.2: a failed push is inline, with Retry. */
  pushFailed?: boolean
  /** AUDIO-30: injected, so every gain change goes through Web Audio. */
  previewGain?: PreviewGainStage | null
  /** Normalised 0–1 peaks; a deterministic placeholder when omitted. */
  peaks?: readonly number[]
  onPreviewToggle: () => void
  onPreviewSeek: (positionMs: number) => void
  onPreviewVolume: (volume: number) => void
  onPreviewMute: (muted: boolean) => void
  /** Absent for a one-shot (AUDIO-3). */
  onLoopChange?: (loop: boolean) => void
  onPlayToTable: () => void
  onStop: () => void
  onRetryPush?: () => void
  /** §12.2: offered beside `This file couldn't be processed`. */
  onReplace?: () => void
  className?: string
  style?: React.CSSProperties
}

export function AudioCue({
  cue,
  preview,
  loop = true,
  live = false,
  tableHearing = null,
  playability = 'ready',
  connection = 'connected',
  pushFailed = false,
  previewGain = null,
  peaks,
  onPreviewToggle,
  onPreviewSeek,
  onPreviewVolume,
  onPreviewMute,
  onLoopChange,
  onPlayToTable,
  onStop,
  onRetryPush,
  onReplace,
  className,
  style,
}: AudioCueProps): React.JSX.Element {
  const titleId = React.useId()
  const durationMs = cue.duration_ms
  const positionMs = clampPositionMs(preview.positionMs, durationMs)
  const volume = clampUnit(preview.volume)
  const looping = isLoopable(cue.kind) && loop
  const bars = peaks && peaks.length > 0 ? peaks : fallbackPeaks(BAR_COUNT, durationMs)
  const played = durationMs > 0 ? Math.round((bars.length * positionMs) / durationMs) : 0
  const playable = playability === 'ready'

  // AUDIO-30: the stage is the only place gain is ever set. `element.volume` is
  // never written anywhere in this component.
  React.useEffect(() => {
    previewGain?.setGain(preview.muted ? 0 : volume)
  }, [previewGain, preview.muted, volume])

  const status = cueStatusMessage({
    playability,
    live,
    connection,
    pushFailed,
    previewBlocked: preview.blocked === true,
  })

  function handlePreviewToggle() {
    // The context may only be created from a gesture, and this is the gesture.
    previewGain?.unlock()
    onPreviewToggle()
  }

  function handleSeekKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.altKey || event.ctrlKey || event.metaKey) return
    const next = nextSeekPositionMs(event.key, positionMs, durationMs)
    if (next === null) return
    event.preventDefault()
    onPreviewSeek(next)
  }

  const classes = ['gm-audio-cue', className].filter(Boolean).join(' ')

  return (
    <section
      className={classes}
      style={style}
      aria-labelledby={titleId}
      data-kind={cue.kind}
      data-live={live ? 'true' : 'false'}
    >
      <header className="gm-audio-cue__header">
        <span className="material-symbols-rounded gm-audio-cue__glyph" aria-hidden="true">
          graphic_eq
        </span>
        <h3 className="gm-audio-cue__title" id={titleId}>
          {cue.title}
        </h3>
        {/* AUDIO-4: the kind is stated, never offered as a control. */}
        <span className="gm-audio-cue__kind">{kindSummary(cue.kind, looping)}</span>
        {live && <Badge tone="primary">LIVE</Badge>}
        <span className="gm-audio-cue__duration">{formatClock(durationMs)}</span>
      </header>

      {/* ── Private preview (AUDIO-5 to AUDIO-7) ── */}
      <div className="gm-audio-cue__preview" role="group" aria-label="Preview (only you hear this)">
        <button
          type="button"
          className="gm-audio-cue__transport gm-audio-cue__control"
          onClick={handlePreviewToggle}
          disabled={!playable}
          aria-label={`${preview.playing ? 'Pause' : 'Play'} preview of ${cue.title}`}
          data-touch-target="true"
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            {preview.playing ? 'pause' : 'play_arrow'}
          </span>
        </button>

        <div className="gm-audio-cue__seek">
          {/* AUDIO-10: the bars are decoration; the slider is the control. */}
          <div className="gm-audio-cue__bars" aria-hidden="true">
            {bars.map((peak, index) => (
              <span
                key={index}
                className={
                  index < played ? 'gm-audio-cue__bar gm-audio-cue__bar--played' : 'gm-audio-cue__bar'
                }
                style={{ height: `${Math.max(8, Math.round(clampUnit(peak) * 100))}%` }}
              />
            ))}
          </div>
          <input
            type="range"
            className="gm-audio-cue__slider"
            min={0}
            max={durationMs}
            step={SEEK_STEP_MS}
            value={positionMs}
            disabled={!playable}
            aria-label={`Seek ${cue.title}`}
            aria-valuetext={seekValueText(positionMs, durationMs)}
            onKeyDown={handleSeekKeyDown}
            onChange={(event) => onPreviewSeek(clampPositionMs(Number(event.target.value), durationMs))}
          />
          <p className="gm-audio-cue__times" aria-hidden="true">
            <span>{formatClock(positionMs)}</span>
            <span>{formatRemaining(positionMs, durationMs)}</span>
          </p>
        </div>
      </div>

      <div className="gm-audio-cue__local" role="group" aria-label="Your device">
        <span className="material-symbols-rounded gm-audio-cue__glyph" aria-hidden="true">
          volume_up
        </span>
        <input
          type="range"
          className="gm-audio-cue__volume"
          min={0}
          max={1}
          step={0.01}
          value={volume}
          aria-label="Preview volume"
          aria-valuetext={volumeValueText(volume)}
          onChange={(event) => onPreviewVolume(clampUnit(Number(event.target.value)))}
        />
        <button
          type="button"
          className="gm-audio-cue__toggle gm-audio-cue__control"
          onClick={() => onPreviewMute(!preview.muted)}
          aria-pressed={preview.muted}
          aria-label={`Mute preview of ${cue.title}`}
          data-touch-target="true"
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            {preview.muted ? 'volume_off' : 'volume_up'}
          </span>
        </button>

        {/* AUDIO-3: rendered for ambience only — a one-shot has no loop control. */}
        {isLoopable(cue.kind) && (
          <button
            type="button"
            className="gm-audio-cue__toggle gm-audio-cue__loop gm-audio-cue__control"
            onClick={() => onLoopChange?.(!loop)}
            aria-pressed={loop}
            aria-label={`Loop ${cue.title}`}
            data-touch-target="true"
          >
            <span className="material-symbols-rounded" aria-hidden="true">
              repeat
            </span>
            <span>Loop</span>
          </button>
        )}
      </div>

      {/* AUDIO-6: previewing another cue pauses the monitor, not the table. */}
      {tableHearing !== null && tableHearing !== '' && (
        <p className="gm-audio-cue__monitor">Table still hearing: {tableHearing}</p>
      )}

      {/* ── The table's two controls (AUDIO-8) ── */}
      <div className="gm-audio-cue__table" role="group" aria-label="Table controls">
        <Button
          variant="filled"
          size="small"
          icon="campaign"
          onClick={onPlayToTable}
          disabled={!canPlayToTable(playability, connection)}
        >
          Play to table
        </Button>
        {/* AUDIO-9: this Stop names its cue, so it can never silence a newer
            one — and it is NEVER disabled (X-3): a push is in flight before this
            client calls it live, and the server ignores a Stop that finds
            nothing. A native button, because the accessible name has to carry
            the title while the visible label stays short (ds Button forwards no
            ARIA attributes). */}
        <button
          type="button"
          className="gm-audio-cue__stop gm-audio-cue__control"
          onClick={onStop}
          aria-label={`Stop ${cue.title}`}
          data-touch-target="true"
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            stop_circle
          </span>
          <span>Stop</span>
        </button>
        {/* STATE-2: a failed push names its next action, and resends the same
            command rather than starting a second one (STATE-3). */}
        {pushFailed && onRetryPush && (
          <Button variant="text" size="small" onClick={onRetryPush}>
            Retry
          </Button>
        )}
      </div>

      {/* STATE-7: the one polite region on this surface. Its text is derived
          from discrete state only, so no tick and no seek step announces. */}
      <p className="gm-audio-cue__status" role="status">
        {status}
      </p>

      {playability === 'failed' && onReplace && (
        <div className="gm-audio-cue__recovery">
          <Button variant="text" size="small" onClick={onReplace}>
            Replace
          </Button>
        </div>
      )}
    </section>
  )
}
