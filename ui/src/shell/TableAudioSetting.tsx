import { useState } from 'react'
import { presenceSentence } from '../gm/audioHelpers'
import { useCuePlayback } from './cuePlayback'
import { useTableSession } from './tableSession'

export function TableAudioSetting() {
  const { playback, audioEnabled, connected, presence } = useCuePlayback()
  const { liveSession } = useTableSession()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(false)
  const enabled = audioEnabled ?? liveSession?.audioEnabled
  if (!playback || !liveSession || enabled === undefined) return null
  return (
    <div className="session-control__audio">
      <label>
        <input type="checkbox" role="switch" checked={enabled} disabled={pending || (!enabled && !connected)}
          onChange={async (event) => {
            const next = event.currentTarget.checked
            setPending(true)
            setError(false)
            try { setError(!await playback.setEnabled(next)) }
            finally { setPending(false) }
          }} /> Table audio
      </label>
      {enabled && <p>{presence ? presenceSentence(presence) : 'Listener status unavailable.'}</p>}
      {error && <p role="alert">Couldn’t change table audio. Check the session and try again.</p>}
    </div>
  )
}
