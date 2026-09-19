/**
 * AudioConsentPrompt — the TABLE client's sound gate and the player's mute.
 *
 * This is the one component here that a player sees, so it is also the one
 * bound by AUDIO-29: it is never given a cue's title or filename, and its
 * props have nowhere to put one.
 *
 *  - AUDIO-18  consent is one tap PER PAGE LOAD, held in memory only. This
 *              component is controlled and writes no storage of any kind; a
 *              reload therefore asks again, which is the documented boundary.
 *  - AUDIO-19  while table audio is on a joining client shows the small
 *              `Tap to enable sound` banner at once; if a cue arrives before
 *              the tap, the full prompt appears instead.
 *  - AUDIO-20  mute is a player toggle, and §12.2 keeps it available even while
 *              the browser cannot play the sound.
 *  - X-10      no remote subresource: the icon is a font ligature already in
 *              the bundle.
 */

import * as React from 'react'
import './AudioConsentPrompt.css'
import type { ConsentStatus } from './audioHelpers'
import { consentStatusMessage } from './audioHelpers'

export interface AudioConsentPromptProps {
  /** AUDIO-18: the tap has happened on THIS page load. */
  accepted: boolean
  /** AUDIO-19: a cue is waiting on the tap, so show the full prompt. */
  cueWaiting?: boolean
  /** AUDIO-20. */
  muted: boolean
  /** §12.2, audio (player). */
  status?: ConsentStatus
  onAccept: () => void
  onMuteChange: (muted: boolean) => void
  className?: string
  style?: React.CSSProperties
}

const ACCEPT_LABEL = 'Tap to enable sound'
const ACCEPT_DETAIL = 'Your GM is playing something'

export function AudioConsentPrompt({
  accepted,
  cueWaiting = false,
  muted,
  status = 'idle',
  onAccept,
  onMuteChange,
  className,
  style,
}: AudioConsentPromptProps): React.JSX.Element {
  const variant = accepted ? 'accepted' : cueWaiting ? 'prompt' : 'banner'
  const message = consentStatusMessage(status)
  const classes = ['gm-audio-consent', className].filter(Boolean).join(' ')

  return (
    <section className={classes} style={style} data-variant={variant} aria-label="Table sound">
      {!accepted && (
        <button
          type="button"
          className="gm-audio-consent__accept"
          onClick={onAccept}
          data-touch-target="true"
        >
          <span className="gm-audio-consent__disc" aria-hidden="true">
            <span className="material-symbols-rounded">volume_up</span>
          </span>
          <span className="gm-audio-consent__copy">
            <span className="gm-audio-consent__label">{ACCEPT_LABEL}</span>
            {cueWaiting && <span className="gm-audio-consent__detail">{ACCEPT_DETAIL}</span>}
          </span>
        </button>
      )}

      {/* AUDIO-20: after the tap the player's only control is mute, and §12.2
          keeps it available in every state, the error one included. */}
      {accepted && (
        <button
          type="button"
          className="gm-audio-consent__mute"
          onClick={() => onMuteChange(!muted)}
          aria-pressed={muted}
          data-touch-target="true"
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            {muted ? 'volume_off' : 'volume_up'}
          </span>
          <span>Mute</span>
        </button>
      )}

      {/* STATE-7: the one polite region on this surface. */}
      <p className="gm-audio-consent__status" role="status">
        {message}
      </p>
    </section>
  )
}
