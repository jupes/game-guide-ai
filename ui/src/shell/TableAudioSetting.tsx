import { presenceSentence } from '../gm/audioHelpers'
import { useCuePlayback } from './cuePlayback'
import { useTableSession } from './tableSession'

export function TableAudioSetting() {
  const { playback, audioEnabled, connected, presence, enabling, disabling, settingError } = useCuePlayback()
  const { liveSession } = useTableSession()
  const enabled = audioEnabled ?? liveSession?.audioEnabled
  if (!playback || !liveSession || enabled === undefined) return null
  return (
    <div className="session-control__audio">
      <label>
        <input type="checkbox" role="switch" checked={enabling || (enabled && !disabling)}
          disabled={disabling || (!enabled && !enabling && !connected)}
          onChange={(event) => { void playback.setEnabled(event.currentTarget.checked) }} /> Table audio
      </label>
      {enabled && <p>{presence ? presenceSentence(presence) : 'Listener status unavailable.'}</p>}
      {(enabling || disabling) && <p role="status">{disabling ? 'Turning table audio off…' : 'Turning table audio on…'}</p>}
      {settingError && <p role="alert">{settingError}</p>}
    </div>
  )
}
