import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react'
import { notifyUnauthorized } from '../api'
import { CONTRACT_VERSION, GmEventSchema, type CuePlayRequest, type CueStopRequest, type GmEvent } from '../gm/contracts'
import { AudioLiveStrip } from '../gm/AudioLiveStrip'
import { useCampaign } from './campaignContext'
import { useLibraryPanel } from './libraryPanel'
import { useTableSession } from './tableSession'
import { mintCommandId } from './tableSessionApi'

type Frame = Extract<GmEvent, { event: 'audio' }>
interface State {
  frames: readonly Frame[]
  connected: boolean
  pendingPlay: boolean
  pendingStops: number
  failedPlay: CuePlayRequest | null
  stopError: string | null
}
const EMPTY: State = { frames: [], connected: false, pendingPlay: false, pendingStops: 0, failedPlay: null, stopError: null }
const POLL_MS = 2000

function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', finish)
      resolve()
    }
    const timer = setTimeout(finish, delay)
    signal.addEventListener('abort', finish, { once: true })
    if (signal.aborted) finish()
  })
}

// eslint-disable-next-line react-refresh/only-export-components -- separately exercised command and ordering state
export class CuePlayback {
  private state: State = EMPTY
  private listeners = new Set<() => void>()
  private controller: AbortController | null = null
  private latestRead = 0
  private nextRead = 0
  private stopRevision = 0
  private unconfirmedStops = new Set<string | null>()
  private timer?: ReturnType<typeof setTimeout>
  private readonly path: string
  private readonly session: string
  private readonly fetchImpl: typeof fetch

  constructor(campaign: string, session: string, fetchImpl: typeof fetch = fetch) {
    this.session = session
    this.fetchImpl = fetchImpl
    this.path = `/campaigns/${encodeURIComponent(campaign)}/sessions/${encodeURIComponent(session)}/audio`
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  snapshot = () => this.state

  private patch(change: Partial<State>): void {
    this.state = { ...this.state, ...change }
    for (const listener of this.listeners) listener()
  }

  start(): () => void {
    const controller = new AbortController()
    this.controller = controller
    const poll = async () => {
      try { await this.send('', undefined, controller.signal) }
      catch { if (!controller.signal.aborted) this.patch({ connected: false }) }
      if (!controller.signal.aborted) this.timer = setTimeout(() => void poll(), POLL_MS)
    }
    void poll()
    return () => {
      controller.abort()
      clearTimeout(this.timer)
      this.controller = null
      this.unconfirmedStops.clear()
      this.patch(EMPTY)
    }
  }

  private async send(action: string, body: CuePlayRequest | CueStopRequest | undefined, signal: AbortSignal): Promise<number> {
    const read = ++this.nextRead
    const reply = await this.fetchImpl(this.path + action, {
      method: body ? 'POST' : 'GET', credentials: 'include', cache: 'no-store',
      signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
      ...(body && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    })
    signal.throwIfAborted()
    if (reply.status === 401) notifyUnauthorized()
    if (!reply.ok) {
      if (read >= this.latestRead) {
        if (reply.status === 401 || reply.status === 403 || reply.status === 404) {
          this.latestRead = read
          this.patch({ frames: [], connected: false })
        } else if (!action) this.patch({ connected: false })
      }
      return reply.status
    }
    const raw: unknown = await reply.json()
    signal.throwIfAborted()
    if (!Array.isArray(raw) || raw.length !== 2) throw new Error('Invalid audio state')
    const frames = raw.map((item): Frame => {
      const frame = GmEventSchema.parse(item)
      if (frame.event !== 'audio' || frame.session_id !== this.session) throw new Error('Invalid audio frame')
      return frame
    })
    if (frames[0].slot !== 'ambience' || frames[1].slot !== 'one_shot' ||
      frames[0].gen !== frames[1].gen || frames[0].audio_epoch !== frames[1].audio_epoch) throw new Error('Mixed audio state')
    const old = this.state.frames[0]
    const newer = !old ? read >= this.latestRead : frames[0].gen > old.gen ||
      (frames[0].gen === old.gen && (frames[0].audio_epoch > old.audio_epoch ||
       (frames[0].audio_epoch === old.audio_epoch && frames.every((f, i) => f.seq >= this.state.frames[i].seq) &&
        (read >= this.latestRead || frames.some((f, i) => f.seq > this.state.frames[i].seq)))))
    if (newer) {
      this.latestRead = Math.max(read, this.latestRead)
      this.patch({ frames, connected: true })
    }
    return reply.status
  }

  async play(cueId: string, loop: boolean, retry = false): Promise<void> {
    const signal = this.controller?.signal
    const { frames, connected, pendingPlay, pendingStops, failedPlay } = this.state
    if (!signal || !connected || pendingPlay || pendingStops || !frames.length) return
    const body: CuePlayRequest = retry && failedPlay?.cue_id === cueId ? failedPlay : {
      schema_version: CONTRACT_VERSION, command_id: mintCommandId(), cue_id: cueId, loop, audio_epoch: frames[0].audio_epoch,
    }
    const stops = this.stopRevision
    this.patch({ pendingPlay: true, failedPlay: null, stopError: null })
    try {
      const status = await this.send('/play', body, signal)
      if (status >= 400 && stops === this.stopRevision) {
        this.patch({ failedPlay: status === 409 ? null : body, stopError: status === 409 ? 'Table audio changed. Check the live cues before playing again.' : null })
        if (status === 409) await this.send('', undefined, signal)
      }
    } catch {
      if (!signal.aborted && stops === this.stopRevision) this.patch({ failedPlay: body })
    } finally {
      if (!signal.aborted) this.patch({ pendingPlay: false })
    }
  }

  async stop(cueId: string | null): Promise<void> {
    const signal = this.controller?.signal
    if (!signal || this.unconfirmedStops.has(cueId)) return
    this.unconfirmedStops.add(cueId)
    const body: CueStopRequest = { schema_version: CONTRACT_VERSION, command_id: mintCommandId(), cue_id: cueId }
    const revision = ++this.stopRevision
    this.patch({ pendingStops: this.state.pendingStops + 1, failedPlay: null, stopError: null })
    try {
      let delay = 500
      while (!signal.aborted) {
        const status = await this.send('/stop', body, signal).catch(() => 0)
        if (signal.aborted) break
        if (status >= 200 && status < 300) {
          if (revision === this.stopRevision) this.patch({ stopError: null })
          break
        }
        if (status >= 400 && status < 500 && status !== 408 && status !== 429) {
          if (revision === this.stopRevision) this.patch({ stopError: "Couldn't confirm Stop. Check the live session." })
          break
        }
        if (revision === this.stopRevision) this.patch({ stopError: 'Reconnecting to stop table audio…' })
        await waitForRetry(delay, signal)
        delay = Math.min(delay * 2, 5000)
      }
    } finally {
      if (!signal.aborted) {
        this.unconfirmedStops.delete(cueId)
        this.patch({ pendingStops: this.state.pendingStops - 1 })
      }
    }
  }
}

const Context = createContext<CuePlayback | null>(null)
const idleSnapshot = () => EMPTY
const idleSubscribe = () => () => {}

export function CuePlaybackProvider({ children }: { children: ReactNode }) {
  const live = useTableSession().liveSession
  const allowed = useLibraryPanel().cueMediaEnabled
  const scope = useCampaign().scope?.key
  const campaign = live?.campaignId
  const session = live?.sessionId
  const playback = useMemo(() => allowed && scope && campaign && session ? new CuePlayback(campaign, session) : null,
    [allowed, scope, campaign, session])
  useEffect(() => playback?.start(), [playback])
  return <Context.Provider value={playback}>{children}</Context.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- hook shares the provider's store
export function useCuePlayback() {
  const playback = useContext(Context)
  const state = useSyncExternalStore(playback?.subscribe ?? idleSubscribe, playback?.snapshot ?? idleSnapshot)
  return { playback, ...state }
}

export function LiveCueControls() {
  const { playback, frames, connected, stopError } = useCuePlayback()
  if (!playback) return null
  const slots = frames.flatMap(({ slot, playing }) => playing ? [{ slot, title: playing.title, loop: playing.loop }] : [])
  return <>
    <AudioLiveStrip slots={slots} connection={connected ? 'connected' : 'reconnecting'}
      onStopSlot={(slot) => {
        const playing = frames.find((frame) => frame.slot === slot)?.playing
        if (playing) void playback.stop(playing.cue_id)
      }}
      onStopAll={() => void playback.stop(null)} />
    {stopError && <p role="alert">{stopError}</p>}
  </>
}
