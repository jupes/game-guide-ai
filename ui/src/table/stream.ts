import { CONTRACT_VERSION, TableEventSchema, TableSnapshotSchema, type TableEvent, type TableSlot } from '../gm/contracts'
import { readEventStream } from '../gm/readEventStream'

export type AudioFrame = Extract<TableEvent, { event: 'audio' }>
export type TablePicture = {
  kind: 'live'; slots: TableSlot[]
  audioEnabled: boolean; audio: AudioFrame[]; clockOffsetMs: number; audioContext: string | null
}
type Snapshot = TablePicture | { kind: 'inactive' | 'retry' }
type StreamEnd = { kind: 'inactive' | 'signed-out' | 'reconnect' | 'retry' } | { kind: 'full'; delay: number }

const SNAPSHOT_FRAME_LIMIT = 50

/** HTTP Date has one-second precision. Estimate server time at the midpoint of the request. */
export function responseClock(response: Response, sentAt: number): number {
  const server = Date.parse(response.headers.get('Date') ?? '')
  return Number.isFinite(server) ? server + 500 - (sentAt + Date.now()) / 2 : 0
}

export function tablePicture(raw: unknown, clockOffsetMs = 0, audioContext: string | null = null): Snapshot {
  const parsed = TableSnapshotSchema.safeParse(raw)
  if (!parsed.success) return { kind: 'retry' }
  const { frames } = parsed.data
  const session = frames.find((frame) => frame.event === 'session')
  const picture = frames.find((frame) => frame.event === 'snapshot')
  if (session === undefined) return { kind: 'inactive' }
  if (picture === undefined) return { kind: 'retry' }
  const audio = frames.filter((frame): frame is AudioFrame => frame.event === 'audio')
  if (new Set(audio.map((frame) => frame.slot)).size !== audio.length || (!session.audio && audio.some((f) => f.playing))) {
    return { kind: 'retry' }
  }
  return { kind: 'live', slots: picture.slots,
    audioEnabled: session.audio, audio, clockOffsetMs,
    audioContext: audioContext !== null && /^[a-f0-9]{64}$/.test(audioContext) ? audioContext : null }
}

type StreamUpdates = {
  opened(): void
  bytes(): void
  picture(picture: TablePicture): void
  hidden?(): boolean
}

export async function streamTable(
  campaignId: string,
  signal: AbortSignal,
  request: typeof fetch,
  updates: StreamUpdates,
): Promise<StreamEnd> {
  try {
    const sentAt = Date.now()
    const response = await request(`/table/stream?campaign_id=${encodeURIComponent(campaignId)}`, {
      headers: { Accept: 'text/event-stream' },
      cache: 'no-store',
      signal,
    })
    if (signal.aborted) return { kind: 'retry' }
    if (response.status === 401) return { kind: 'signed-out' }
    if (response.status === 403) return { kind: 'inactive' }
    const retryAfter = response.headers.get('Retry-After')
    if (response.status === 503 && retryAfter !== null) {
      const seconds = Number(retryAfter)
      return { kind: 'full', delay: Number.isFinite(seconds) ? Math.max(1000, Math.min(seconds * 1000, 300_000)) : 5000 }
    }
    const contentType = response.headers.get('Content-Type')?.split(';', 1)[0].trim().toLowerCase()
    if (!response.ok || response.body === null || contentType !== 'text/event-stream') return { kind: 'retry' }
    const clockOffsetMs = responseClock(response, sentAt)
    updates.opened()
    const pending: TableEvent[] = []
    let picture: TablePicture | null = null
    let end: StreamEnd = { kind: 'retry' }
    await readEventStream(response.body, signal, updates.bytes, (name, data) => {
      const parsed = TableEventSchema.safeParse(JSON.parse(data))
      if (!parsed.success || parsed.data.event !== name) return false
      const frame = parsed.data
      if (frame.event === 'inactive') {
        end = { kind: 'inactive' }
        return false
      }
      if (frame.event === 'reconnect') {
        end = { kind: 'reconnect' }
        return false
      }
      if (picture === null) {
        if (
          pending.length >= SNAPSHOT_FRAME_LIMIT || frame.event === 'slot'
          || (frame.event === 'snapshot' && pending.some((event) => event.event === 'snapshot'))
        ) return false
        pending.push(frame)
        if (frame.event !== 'ready') return true
        const snapshot = tablePicture({ schema_version: CONTRACT_VERSION, frames: pending }, clockOffsetMs,
          response.headers.get('X-Table-Audio-Context'))
        pending.length = 0
        if (snapshot.kind !== 'live') return false
        picture = snapshot
        if (updates.hidden?.()) picture.slots = picture.slots.map((slot) => ({ ...slot, content: null }))
        updates.picture(picture)
        return true
      }
      if (frame.event === 'audio') {
        if (!picture.audioEnabled) return false
        const mark = picture.audio.find((item) => item.slot === frame.slot)?.seq ?? 0
        if (frame.seq <= mark) return true
        if (frame.seq !== mark + 1) {
          end = { kind: 'reconnect' }
          return false
        }
        picture = { ...picture, audio: [...picture.audio.filter((item) => item.slot !== frame.slot), frame] }
        updates.picture(picture)
        return true
      }
      if (frame.event !== 'slot') return false
      const previous = picture.slots.find((slot) => slot.slot === frame.slot)
      if (previous === undefined) return false
      if (frame.seq <= previous.seq) return true
      if (frame.seq !== previous.seq + 1) {
        end = { kind: 'reconnect' }
        return false
      }
      const next: TableSlot = { slot: frame.slot, seq: frame.seq, content: updates.hidden?.() ? null : frame.content }
      picture = { ...picture, slots: picture.slots.map((slot) => slot.slot === next.slot ? next : slot) }
      updates.picture(picture)
      return true
    })
    return end
  } catch {
    return { kind: 'retry' }
  }
}
