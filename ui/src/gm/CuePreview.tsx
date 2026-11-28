import * as React from 'react'

import { AudioCue } from './AudioCue'
import { readPreviewVolume, usePreviewGain, writePreviewVolume } from './audioGain'
import { clampPositionMs } from './audioHelpers'
import type { Cue } from './contracts'
import { previewPeaks } from './cueApi'
import { assetPath } from './documentFields'
import { useCuePlayback } from '../shell/cuePlayback'
import { useTableSession } from '../shell/tableSession'

let activePreview: HTMLAudioElement | null = null
const LOCAL_SOUND_EVENT = 'aetheril:local-cue-sound'

export function CuePreview({ cue, fetchImpl }: { cue: Cue; fetchImpl: typeof fetch }): React.JSX.Element {
  const { playback, frames, connected, pendingPlay, pendingStops, failedPlay, audioEnabled: observed } = useCuePlayback()
  const liveSession = useTableSession().liveSession
  const audioEnabled = observed ?? (liveSession?.audioEnabled === true)
  const [media, setMedia] = React.useState<HTMLAudioElement | null>(null)
  const audioRef = React.useRef<HTMLAudioElement | null>(null)
  const linkMedia = React.useCallback((node: HTMLAudioElement | null) => {
    audioRef.current = node
    setMedia(node)
  }, [])
  const [volume, setVolume] = React.useState(readPreviewVolume)
  const [muted, setMuted] = React.useState(playback?.monitor.isMuted ?? false)
  const [loop, setLoop] = React.useState(cue.kind === 'ambience')
  const [playing, setPlaying] = React.useState(false)
  const [positionMs, setPosition] = React.useState(0)
  const [blocked, setBlocked] = React.useState(false)
  const [failed, setFailed] = React.useState(false)
  const [peaks, setPeaks] = React.useState<readonly number[]>()
  const seekOnLoad = React.useRef(0)
  const previewGain = usePreviewGain({ media, volume, muted })
  const live = frames.some((frame) => frame.playing?.cue_id === cue.cue_id)
  const controls = !cue.archived && playback !== null

  React.useEffect(() => {
    const onChange = (event: Event) => {
      const detail = (event as CustomEvent<{ volume?: number; muted?: boolean }>).detail
      if (typeof detail?.volume === 'number') setVolume(detail.volume)
      if (typeof detail?.muted === 'boolean') setMuted(detail.muted)
    }
    window.addEventListener(LOCAL_SOUND_EVENT, onChange)
    return () => window.removeEventListener(LOCAL_SOUND_EVENT, onChange)
  }, [])

  React.useEffect(() => {
    const abort = new AbortController()
    void previewPeaks(cue, abort.signal, fetchImpl).then((values) => {
      if (!abort.signal.aborted) setPeaks(values)
    })
    return () => abort.abort()
  }, [cue, fetchImpl])

  React.useEffect(() => () => {
    if (activePreview === media) activePreview = null
    if (media) playback?.monitor.previewEnded(media)
    if (!media?.getAttribute('src')) return
    media.pause()
    media.removeAttribute('src')
    media.load()
  }, [media, playback])

  function seek(ms: number): void {
    const position = clampPositionMs(ms, cue.duration_ms)
    seekOnLoad.current = position
    const element = audioRef.current
    if (element && element.readyState > 0) element.currentTime = position / 1000
    setPosition(position)
  }

  function toggle(): void {
    const element = audioRef.current
    if (element === null) return
    if (!element.paused) {
      element.pause()
      return
    }
    setBlocked(false)
    setFailed(false)
    if (!element.getAttribute('src') || element.error) {
      element.src = assetPath(cue.campaign_id, cue.asset_id)
    }
    if (activePreview !== element) activePreview?.pause()
    activePreview = element
    void element.play().catch((error: unknown) => {
      if (activePreview === element) activePreview = null
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (error instanceof DOMException && error.name === 'NotAllowedError') setBlocked(true)
      else setFailed(true)
    })
  }

  return (
    <>
      <audio
        ref={linkMedia}
        preload="none"
        muted={muted}
        loop={loop && cue.kind === 'ambience'}
        onPlay={(event) => { playback?.monitor.previewStarted(event.currentTarget); setPlaying(true) }}
        onPause={(event) => {
          if (activePreview === event.currentTarget) activePreview = null
          playback?.monitor.previewEnded(event.currentTarget)
          setPlaying(false)
        }}
        onEnded={(event) => {
          if (activePreview === event.currentTarget) activePreview = null
          playback?.monitor.previewEnded(event.currentTarget)
          setPlaying(false)
          seek(0)
        }}
        onLoadedMetadata={(event) => { event.currentTarget.currentTime = seekOnLoad.current / 1000 }}
        onTimeUpdate={(event) => setPosition(event.currentTarget.currentTime * 1000)}
        onError={(event) => { playback?.monitor.previewEnded(event.currentTarget); setPlaying(false); setFailed(true) }}
      />
      <AudioCue
        cue={cue}
        preview={{ playing, positionMs, muted, volume, blocked }}
        previewGain={previewGain}
        live={live}
        tableHearing={playing && !live ? frames.flatMap(({ playing: heard }) => heard ? [heard.title] : []).join(', ') : null}
        connection={controls && !connected ? 'reconnecting' : 'connected'}
        pushDisabled={!audioEnabled || pendingPlay || pendingStops > 0}
        pushFailed={failedPlay?.cue_id === cue.cue_id}
        onPlayToTable={controls ? () => void playback.play(cue.cue_id, loop) : undefined}
        onRetryPush={controls ? () => void playback.play(cue.cue_id, loop, true) : undefined}
        onStop={controls ? () => void playback.stop(cue.cue_id) : undefined}
        loop={loop}
        peaks={peaks}
        onPreviewToggle={toggle}
        onPreviewSeek={seek}
        onPreviewVolume={(next) => {
          writePreviewVolume(next)
          playback?.monitor.setVolume(next)
          window.dispatchEvent(new CustomEvent(LOCAL_SOUND_EVENT, { detail: { volume: next } }))
        }}
        onPreviewMute={(next) => {
          playback?.monitor.setMuted(next)
          window.dispatchEvent(new CustomEvent(LOCAL_SOUND_EVENT, { detail: { muted: next } }))
        }}
        onLoopChange={setLoop}
      />
      {failed && <p role="alert">Couldn't play this sound. Try preview again.</p>}
    </>
  )
}
