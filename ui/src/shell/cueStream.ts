import { CONTRACT_VERSION, GmEventSchema, GmSnapshotSchema, type GmEvent } from '../gm/contracts'
import { readEventStream } from '../gm/readEventStream'

export type AudioFrame = Extract<GmEvent, { event: 'audio' }>
export type PresenceFrame = Extract<GmEvent, { event: 'presence' }>
type SessionFrame = Extract<GmEvent, { event: 'session' }>['session']
type End = 'retry' | 'reconnect' | 'gone' | 'signed-out' | { retryAfterMs: number }
export const CUE_STATE_SILENCE_MS = 10_000

export function audioPair(raw: unknown, session: string): AudioFrame[] {
  if (!Array.isArray(raw) || raw.length !== 2) throw new Error('Incomplete audio state')
  const frames = raw.map((item): AudioFrame => {
    const frame = GmEventSchema.parse(item)
    if (frame.event !== 'audio' || frame.session_id !== session) throw new Error('Invalid audio frame')
    return frame
  })
  if (frames[0].slot !== 'ambience' || frames[1].slot !== 'one_shot' ||
      frames[0].gen !== frames[1].gen || frames[0].audio_epoch !== frames[1].audio_epoch) {
    throw new Error('Mixed audio state')
  }
  return frames
}

/** One atomic opening snapshot followed by complete, independently sequenced audio pairs. */
export async function streamCueState(campaign: string, session: string, request: typeof fetch, signal: AbortSignal,
  apply: (pair: AudioFrame[], initial: boolean) => boolean,
  observe?: { session(value: SessionFrame): void; presence(value: PresenceFrame | null): void; clock(offsetMs: number): void },
): Promise<End> {
  const deadline = new AbortController()
  const timed = AbortSignal.any([signal, deadline.signal])
  let opening: GmEvent[] | null = []
  let identity: SessionFrame | undefined
  let current: AudioFrame[] = []
  let pending: AudioFrame | null = null
  let end: End = 'retry'
  const expired = () => deadline.abort()
  let frameDeadline = setTimeout(expired, CUE_STATE_SILENCE_MS)
  let idle: ReturnType<typeof setTimeout> | undefined
  try {
    const sentAt = Date.now()
    const reply = await request(`/campaigns/${encodeURIComponent(campaign)}/stream`, {
      headers: { Accept: 'text/event-stream', 'X-Table-Audio-Presence': '1' }, credentials: 'include', cache: 'no-store', signal: timed,
    })
    timed.throwIfAborted()
    if (reply.status === 401) return 'signed-out'
    if (reply.status === 403) return 'gone'
    if (reply.status === 429 || reply.status === 503) {
      const seconds = Number(reply.headers.get('Retry-After'))
      if (Number.isFinite(seconds) && seconds > 0) return { retryAfterMs: seconds * 1000 }
    }
    if (!reply.ok || !reply.body || !reply.headers.get('Content-Type')?.startsWith('text/event-stream')) return 'retry'
    const server = Date.parse(reply.headers.get('Date') ?? '')
    const clockOffset = server + 500 - (sentAt + Date.now()) / 2
    await readEventStream(reply.body, timed, () => {
      clearTimeout(idle)
      idle = setTimeout(expired, CUE_STATE_SILENCE_MS)
    }, (name, data) => {
      const frame = GmEventSchema.parse(JSON.parse(data))
      if (frame.event !== name) return false
      if (frame.event === 'reconnect') {
        end = opening === null && pending === null ? 'reconnect' : 'retry'
        return false
      }
      if (opening !== null) {
        if (opening.length >= 200) return false
        opening.push(frame)
        if (frame.event !== 'ready') return true
        const snapshot = GmSnapshotSchema.parse({ schema_version: CONTRACT_VERSION, frames: opening })
        identity = snapshot.frames.find((f) => f.event === 'session')?.session
        if (identity?.state !== 'live' || identity.campaign_id !== campaign || identity.session_id !== session) {
          end = 'gone'
          return false
        }
        current = audioPair(snapshot.frames.filter((f) => f.event === 'audio'), session)
        if (current[0].gen !== identity.gen || current[0].audio_epoch !== identity.audio_epoch) return false
        const presence = snapshot.frames.filter((item) => item.event === 'presence')
        if (presence.length > 1 || presence.some((item) => item.session_id !== session || item.gen !== identity?.gen)) return false
        if (!apply(current, true)) return false
        clearTimeout(frameDeadline)
        opening = null
        if (Number.isFinite(clockOffset)) observe?.clock(clockOffset)
        observe?.session(identity)
        observe?.presence(presence[0] ?? null)
        return true
      }
      if (frame.event === 'session') {
        if (pending || frame.session.state !== 'live' || frame.session.campaign_id !== campaign ||
            frame.session.session_id !== session || frame.session.gen !== identity?.gen) return false
        if (frame.session.audio_epoch < identity.audio_epoch || frame.session.audio_epoch > current[0].audio_epoch + 1) return false
        if (frame.session.audio_epoch > identity.audio_epoch) frameDeadline = setTimeout(expired, CUE_STATE_SILENCE_MS)
        identity = frame.session
        observe?.session(identity)
        return true
      }
      if (frame.event === 'presence') {
        if (pending || frame.session_id !== session || frame.gen !== identity?.gen) return false
        observe?.presence(frame)
        return true
      }
      if (frame.event !== 'audio') return pending === null && frame.event !== 'ready'
      if (frame.gen !== identity?.gen || frame.session_id !== session || frame.audio_epoch !== identity.audio_epoch) return false
      if (!pending) {
        if (frame.slot !== 'ambience') return false
        pending = frame
        clearTimeout(frameDeadline)
        frameDeadline = setTimeout(expired, CUE_STATE_SILENCE_MS)
        return true
      }
      const pair = audioPair([pending, frame], session)
      if (pair.some((f, i) => f.seq < current[i].seq || f.seq > current[i].seq + 1) || !apply(pair, false)) return false
      current = pair
      pending = null
      clearTimeout(frameDeadline)
      return true
    })
    return end
  } catch {
    return 'retry'
  } finally {
    clearTimeout(frameDeadline)
    clearTimeout(idle)
  }
}
