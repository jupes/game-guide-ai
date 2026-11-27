import { REVEAL_COPY } from '../gm/revealCopy'
import { useCuePlayback } from './cuePlayback'
import { useReveals } from './revealContext'
import { STOP_ALL } from './revealStop'
import './DialogTableStop.css'

/** A modal owns its keyboard and accessibility tree. Keep Stop reachable inside it. */
export function DialogTableStop() {
  const audio = useCuePlayback()
  const reveals = useReveals()
  const sound = audio.playback && (!audio.connected || audio.frames.some((frame) => frame.playing !== null))
  const shown = reveals.status === 'unknown' || reveals.state?.slots.some((slot) => slot.live !== null)
  if (!sound && !shown) return null
  return <button type="button" className="dialog-table-stop" onClick={(event) => {
    const dialog = event.currentTarget.closest<HTMLElement>('[role="dialog"]')
    const heading = dialog?.querySelector<HTMLElement>('h2[tabindex]')
    const fallback = heading ?? dialog
    fallback?.focus()
    reveals.stop(STOP_ALL, REVEAL_COPY.everything)
    void audio.playback?.stop(null)
  }}>Stop all table sharing</button>
}
