/**
 * AudioLiveStrip — what the table is hearing, in the GM's workspace header.
 *
 * AUDIO-31 puts this beside the reveal indicator on every layout and in every
 * channel while any cue is live, so silence stays one action after the cue's
 * card has scrolled away (X-3, X-9). It is Stop-only: there is no Play here.
 *
 *  - AUDIO-1   two slots, one ambience and one one-shot, so at most two rows
 *  - AUDIO-9   this Stop is **Stop all** — it clears both slots, so it silences
 *              whatever is sounding even if a push was in flight
 *  - AUDIO-17  while the GM is disconnected the strip says `Reconnecting…` and
 *              Stop stays enabled; it is never disabled here at all (X-3)
 *  - AUDIO-21  the presence sentence, rendered as ordinary text
 *  - AUDIO-22  presence is never announced on every change, so the counts live
 *              OUTSIDE this surface's one polite region (STATE-7)
 *
 * The strip is GM-facing, so naming the live cues is correct; AUDIO-29's ban on
 * titles applies to table clients, which this is not.
 */

import * as React from 'react'
import './AudioLiveStrip.css'
import { Button } from '../ds/Button'
import type { AudioConnection, AudioSlot, PresenceSummary } from './audioHelpers'
import { kindSummary, presenceSentence, stripStatusMessage } from './audioHelpers'

/** One occupied slot, as the GM's audio frames describe it (AUDIO-11). */
export interface AudioLiveSlot {
  slot: AudioSlot
  title: string
  loop: boolean
}

export interface AudioLiveStripProps {
  /** Nothing, one slot, or both (AUDIO-1). */
  slots: readonly AudioLiveSlot[]
  presence: PresenceSummary
  connection?: AudioConnection
  /** AUDIO-9: clears both slots. Never queued, never confirmed, never disabled. */
  onStopAll: () => void
  /** §10.2: the narrow layout's named list carries a Stop per live cue. */
  onStopSlot?: (slot: AudioSlot) => void
  className?: string
  style?: React.CSSProperties
}

export function AudioLiveStrip({
  slots,
  presence,
  connection = 'connected',
  onStopAll,
  onStopSlot,
  className,
  style,
}: AudioLiveStripProps): React.JSX.Element | null {
  const status = stripStatusMessage(connection)

  // §12.2: live indicators are absent when nothing is live. While the stream is
  // down, liveness is unknown, so the strip stays on screen with its Stop (X-3).
  if (slots.length === 0 && connection === 'connected') return null

  const classes = ['gm-audio-strip', className].filter(Boolean).join(' ')

  return (
    <section
      className={classes}
      style={style}
      aria-label="Live table audio"
      data-connection={connection}
    >
      <span className="gm-audio-strip__pulse" aria-hidden="true">
        <span className="gm-audio-strip__pulse-bar" />
        <span className="gm-audio-strip__pulse-bar" />
        <span className="gm-audio-strip__pulse-bar" />
        <span className="gm-audio-strip__pulse-bar" />
      </span>

      <div className="gm-audio-strip__body">
        {slots.length > 0 && (
          <ul className="gm-audio-strip__slots">
            {slots.map((live) => (
              <li key={live.slot} className="gm-audio-strip__slot" data-slot={live.slot}>
                <span className="gm-audio-strip__slot-title">{live.title}</span>
                <span className="gm-audio-strip__slot-kind">{kindSummary(live.slot, live.loop)}</span>
                {onStopSlot && (
                  /* AUDIO-9: a Stop that names one cue clears its slot only if
                     that cue still holds it. Native, so the accessible name can
                     carry the title while the visible label stays short. */
                  <button
                    type="button"
                    className="gm-audio-strip__slot-stop"
                    onClick={() => onStopSlot(live.slot)}
                    aria-label={`Stop ${live.title}`}
                    data-touch-target="true"
                  >
                    Stop
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        {/* AUDIO-21. Deliberately not inside the live region below (AUDIO-22). */}
        <p className="gm-audio-strip__presence">{presenceSentence(presence)}</p>
      </div>

      {/* AUDIO-9, AUDIO-17, X-3: never disabled, in any state. */}
      <Button variant="text" size="small" icon="stop_circle" onClick={onStopAll}>
        Stop all
      </Button>

      {/* STATE-7: the one polite region on this surface. */}
      <p className="gm-audio-strip__status" role="status">
        {status}
      </p>
    </section>
  )
}
