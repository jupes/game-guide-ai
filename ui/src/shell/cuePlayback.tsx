import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react'
import { notifyUnauthorized } from '../api'
import { AudioSettingsAnswerSchema, CONTRACT_VERSION, GmEventSchema, type CuePlayRequest, type CueStopRequest } from '../gm/contracts'
import { AudioLiveStrip } from '../gm/AudioLiveStrip'
import { useCampaign } from './campaignContext'
import { CueStopCourier, cueStops } from './cueStopCourier'
import { audioPair, streamCueState, type AudioFrame, type PresenceFrame } from './cueStream'
import { CurrentUserContext } from './currentUser'
import { useLibraryPanel } from './libraryPanel'
import { SESSION_REFRESH_EVENT, useTableSession } from './tableSession'
import { mintCommandId } from './tableSessionApi'
import { GmCueMonitor } from './gmCueMonitor'

interface State {
  frames: readonly AudioFrame[]
  connected: boolean
  pendingPlay: boolean
  pendingStops: number
  failedPlay: CuePlayRequest | null
  stopError: string | null
  presence: PresenceFrame | null
  audioEnabled: boolean | null
  clockOffsetMs: number
  enabling: boolean
  disabling: boolean
  settingError: string | null
}
const EMPTY: State = { frames: [], connected: false, pendingPlay: false, pendingStops: 0, failedPlay: null, stopError: null,
  presence: null, audioEnabled: null, clockOffsetMs: 0, enabling: false, disabling: false, settingError: null }
const POLL_MS = 5000

// eslint-disable-next-line react-refresh/only-export-components -- separately exercised command and ordering state
export class CuePlayback {
  private state: State = EMPTY
  private listeners = new Set<() => void>()
  private controller: AbortController | null = null
  private latestRead = 0
  private nextRead = 0
  private stopRevision = 0
  private settingsEpoch = -1
  private settingIntent = 0
  private timer?: ReturnType<typeof setTimeout>
  private readonly path: string
  private readonly campaign: string
  private readonly session: string
  private readonly fetchImpl: typeof fetch
  private readonly account: string
  private readonly courier: CueStopCourier
  readonly monitor: GmCueMonitor

  constructor(campaign: string, session: string, fetchImpl: typeof fetch = fetch,
    account = '', courier = new CueStopCourier()) {
    this.session = session
    this.campaign = campaign
    this.fetchImpl = fetchImpl
    this.account = account
    this.courier = courier
    this.monitor = new GmCueMonitor(campaign, fetchImpl)
    this.path = `/campaigns/${encodeURIComponent(campaign)}/sessions/${encodeURIComponent(session)}/audio`
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  snapshot = () => this.state

  private patch(change: Partial<State>): void {
    this.state = { ...this.state, ...change }
    if ('frames' in change || 'connected' in change || 'audioEnabled' in change || 'clockOffsetMs' in change) {
      this.monitor.update(this.state.frames, this.state.connected, this.state.audioEnabled !== false, this.state.clockOffsetMs)
    }
    for (const listener of this.listeners) listener()
  }

  private settings(enabled: boolean, epoch: number): void {
    if (!Number.isSafeInteger(epoch) || epoch < Math.max(this.settingsEpoch, this.state.frames[0]?.audio_epoch ?? -1)) return
    this.settingsEpoch = epoch
    if (this.state.audioEnabled !== enabled) this.patch({ audioEnabled: enabled })
  }

  private async readPresence(signal: AbortSignal): Promise<void> {
    try {
      const fetchImpl = this.fetchImpl
      const reply = await fetchImpl(this.path + '/presence', {
        credentials: 'include', cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(4000)]),
      })
      if (!reply.ok) throw new Error()
      const frame = GmEventSchema.parse(await reply.json())
      if (frame.event !== 'presence' || frame.session_id !== this.session || frame.gen !== this.state.frames[0]?.gen) throw new Error()
      if (signal.aborted) return
      this.patch({ presence: frame })
      const flag = reply.headers.get('X-Table-Audio-Enabled')
      const epoch = reply.headers.get('X-Table-Audio-Epoch')
      if ((flag === '1' || flag === '0') && epoch && /^\d+$/.test(epoch)) this.settings(flag === '1', Number(epoch))
    } catch {
      if (!signal.aborted) this.patch({ presence: null })
    }
  }

  start(): () => void {
    this.monitor.start()
    const controller = new AbortController()
    this.controller = controller
    let inheritedStops = this.courier.status(this.account, this.path).pendingStops > 0
    const syncStops = () => {
      const { pendingStops, retrying, disabling } = this.courier.status(this.account, this.path)
      this.patch({ pendingStops, disabling, stopError: retrying ? 'Reconnecting to stop table audio…' : null })
      if (inheritedStops && !pendingStops) {
        inheritedStops = false
        refresh()
      }
    }
    let reading: AbortController | null = null
    let failures = 0
    let retryAt = 0
    const read = async () => {
      const request = new AbortController()
      reading = request
      if (Date.now() >= retryAt) {
        const initialRead = ++this.nextRead
        const result = await streamCueState(this.campaign, this.session, this.fetchImpl, request.signal, (pair, initial) => {
          failures = 0
          this.accept(pair, initial ? initialRead : ++this.nextRead)
        }, {
          presence: (presence) => this.patch({ presence }),
          session: (session) => this.settings(session.audio, session.audio_epoch),
          clock: (clockOffsetMs) => this.patch({ clockOffsetMs }),
        })
        if (request.signal.aborted) return
        this.patch({ connected: false, presence: null })
        if (result === 'signed-out' || result === 'gone') {
          this.latestRead = ++this.nextRead
          this.patch({ frames: [] })
          if (result === 'signed-out') notifyUnauthorized()
          else window.dispatchEvent(new Event(SESSION_REFRESH_EVENT))
          return
        }
        if (result === 'reconnect') {
          this.timer = setTimeout(() => void read(), 0)
          return
        }
        const wait = Math.min(30_000, 1000 * 2 ** Math.min(failures++, 5))
        const backoff = Math.min(30_000, wait * (0.75 + Math.random() * 0.5))
        retryAt = Date.now() + Math.max(backoff, typeof result === 'object' ? result.retryAfterMs : 0)
      }
      try {
        const began = Date.now()
        const reply = await this.send('', undefined, request.signal)
        if (reply.ok && !request.signal.aborted) {
          const server = Date.parse(reply.headers.get('Date') ?? '')
          if (Number.isFinite(server)) this.patch({ clockOffsetMs: server + 500 - (began + Date.now()) / 2 })
          await this.readPresence(request.signal)
        }
      }
      catch { if (!request.signal.aborted) this.patch({ connected: false }) }
      if (!request.signal.aborted) this.timer = setTimeout(() => void read(), Math.min(POLL_MS, Math.max(0, retryAt - Date.now())))
    }
    const refresh = () => {
      reading?.abort()
      clearTimeout(this.timer)
      this.patch({ connected: false, presence: null })
      retryAt = failures = 0
      if (!document.hidden && navigator.onLine) void read()
    }
    const unsubscribe = this.courier.subscribe(syncStops)
    syncStops()
    document.addEventListener('visibilitychange', refresh)
    window.addEventListener('online', refresh)
    window.addEventListener('offline', refresh)
    refresh()
    return () => {
      unsubscribe()
      document.removeEventListener('visibilitychange', refresh)
      window.removeEventListener('online', refresh)
      window.removeEventListener('offline', refresh)
      reading?.abort()
      controller.abort()
      clearTimeout(this.timer)
      this.controller = null
      this.patch(EMPTY)
      this.monitor.dispose()
    }
  }

  private async send(action: string, body: CuePlayRequest | CueStopRequest | undefined, signal: AbortSignal): Promise<Response> {
    const read = ++this.nextRead
    const fetchImpl = this.fetchImpl
    const reply = await fetchImpl(this.path + action, {
      method: body ? 'POST' : 'GET', credentials: 'include', cache: 'no-store',
      signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
      ...(body && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    })
    signal.throwIfAborted()
    if (reply.status === 401) notifyUnauthorized()
    if (!this.controller) return reply
    if (!reply.ok) {
      if (read >= this.latestRead) {
        if (reply.status === 401 || reply.status === 403 || reply.status === 404) {
          this.latestRead = read
          this.patch({ frames: [], connected: false, presence: null })
          if (!action && reply.status === 404) window.dispatchEvent(new Event(SESSION_REFRESH_EVENT))
        } else if (!action) this.patch({ connected: false })
      }
      return reply
    }
    const raw: unknown = await reply.json()
    signal.throwIfAborted()
    if (this.controller) this.accept(audioPair(raw, this.session), read)
    return reply
  }

  private accept(frames: AudioFrame[], read: number): void {
    const old = this.state.frames[0]
    const next = frames[0]
    if (next.audio_epoch < this.settingsEpoch) return
    if (!old && read < this.latestRead) return
    if (old && next.gen < old.gen) return
    if (old && next.gen === old.gen) {
      if (next.audio_epoch < old.audio_epoch) return
      for (const [index, frame] of frames.entries()) {
        const previous = this.state.frames[index]
        if (frame.seq < previous.seq || (frame.seq === previous.seq && !previous.playing && frame.playing)) return
      }
      if (next.audio_epoch === old.audio_epoch && read < this.latestRead &&
          frames.every((f, i) => f.seq === this.state.frames[i].seq)) return
    }
    this.latestRead = Math.max(read, this.latestRead)
    this.patch({ frames, connected: true })
  }

  async play(cueId: string, loop: boolean, retry = false): Promise<void> {
    const signal = this.controller?.signal
    const { frames, connected, pendingPlay, pendingStops, failedPlay } = this.state
    if (!signal || !connected || pendingPlay || pendingStops || !frames.length || this.state.audioEnabled === false) return
    const body: CuePlayRequest = retry && failedPlay?.cue_id === cueId ? failedPlay : {
      schema_version: CONTRACT_VERSION, command_id: mintCommandId(), cue_id: cueId, loop,
      audio_epoch: Math.max(this.settingsEpoch, frames[0].audio_epoch),
    }
    const stops = this.stopRevision
    this.patch({ pendingPlay: true, failedPlay: null, stopError: null })
    try {
      const { status } = await this.send('/play', body, signal)
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

  async setEnabled(enabled: boolean): Promise<boolean> {
    const signal = this.controller?.signal
    const head = this.state.frames[0]
    if (!signal || (enabled && (this.state.pendingStops || this.state.enabling || !head || !this.state.connected))) return false
    const intent = ++this.settingIntent
    this.patch({ enabling: enabled, settingError: null })
    const deliver = async (id: string, delivery: AbortSignal) => {
      const fetchImpl = this.fetchImpl
      const reply = await fetchImpl(this.path + '/settings', {
        method: 'POST', credentials: 'include', cache: 'no-store',
        signal: AbortSignal.any([delivery, AbortSignal.timeout(10000)]), headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ schema_version: CONTRACT_VERSION, command_id: id, enabled,
          ...(enabled ? { audio_epoch: Math.max(this.settingsEpoch, head.audio_epoch) } : {}) }),
      })
      if (reply.status === 401 && !delivery.aborted) notifyUnauthorized()
      if (reply.ok) {
        const answer = AudioSettingsAnswerSchema.parse(await reply.json())
        if (!delivery.aborted && !signal.aborted && intent === this.settingIntent) {
          this.settings(answer.enabled, answer.audio_epoch)
          window.dispatchEvent(new Event(SESSION_REFRESH_EVENT))
          void this.send('', undefined, signal).catch(() => {})
        }
      }
      return reply
    }
    try {
      const ok = enabled ? (await deliver(mintCommandId(), signal)).ok
        : await this.courier.disable(this.account, this.path, deliver) === 'acknowledged'
      if (!ok && !signal.aborted && intent === this.settingIntent) this.patch({ settingError: 'Couldn’t change table audio. Check the session and try again.' })
      return ok
    } catch {
      if (!signal.aborted && intent === this.settingIntent) this.patch({ settingError: 'Couldn’t confirm the change. Check the session and try again.' })
      return false
    } finally {
      if (!signal.aborted && intent === this.settingIntent) this.patch({ enabling: false })
    }
  }

  async stop(cueId: string | null): Promise<void> {
    const signal = this.controller?.signal
    if (!signal) return
    const revision = ++this.stopRevision
    this.patch({ failedPlay: null, stopError: null })
    const result = await this.courier.send(this.account, this.path, cueId, (body, delivery) => this.send('/stop', body, delivery))
    if (!signal.aborted && revision === this.stopRevision && result === 'failed') {
      this.patch({ stopError: "Couldn't confirm Stop. Check the live session." })
    }
  }
}

const Context = createContext<CuePlayback | null>(null)
const idleSnapshot = () => EMPTY
const idleSubscribe = () => () => {}

export function CuePlaybackProvider({ children }: { children: ReactNode }) {
  const account = useContext(CurrentUserContext)?.user.id ?? ''
  const live = useTableSession().liveSession
  const allowed = useLibraryPanel().cueMediaEnabled
  const scope = useCampaign().scope?.key
  const campaign = live?.campaignId
  const session = live?.sessionId
  const playback = useMemo(() => allowed && scope && campaign && session ? new CuePlayback(campaign, session, fetch, account, cueStops) : null,
    [allowed, scope, campaign, session, account])
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
  const { playback, frames, connected, stopError, presence } = useCuePlayback()
  if (!playback) return null
  const slots = frames.flatMap(({ slot, playing }) => playing ? [{ slot, title: playing.title, loop: playing.loop }] : [])
  return <>
    <AudioLiveStrip slots={slots} presence={presence ?? undefined} connection={connected ? 'connected' : 'reconnecting'}
      onStopSlot={(slot) => {
        const playing = frames.find((frame) => frame.slot === slot)?.playing
        if (playing) void playback.stop(playing.cue_id)
      }}
      onStopAll={() => void playback.stop(null)} />
    {stopError && <p role="alert">{stopError}</p>}
  </>
}
