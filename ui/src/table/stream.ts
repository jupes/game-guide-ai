import { CONTRACT_VERSION, TableEventSchema, TableSnapshotSchema, type TableEvent, type TableSlot } from '../gm/contracts'
import { readEventStream } from '../gm/readEventStream'

export type TablePicture = { kind: 'live'; participant: boolean; slots: TableSlot[] }
type Snapshot = TablePicture | { kind: 'inactive' | 'retry' }
type StreamEnd = { kind: 'inactive' | 'signed-out' | 'reconnect' | 'retry' } | { kind: 'full'; delay: number }

const SNAPSHOT_FRAME_LIMIT = 50

export function tablePicture(raw: unknown): Snapshot {
  const parsed = TableSnapshotSchema.safeParse(raw)
  if (!parsed.success) return { kind: 'retry' }
  const { frames } = parsed.data
  const session = frames.find((frame) => frame.event === 'session')
  const picture = frames.find((frame) => frame.event === 'snapshot')
  if (session === undefined) return { kind: 'inactive' }
  if (picture === undefined) return { kind: 'retry' }
  return { kind: 'live', participant: session.role === 'participant', slots: picture.slots }
}

type StreamUpdates = {
  opened(): void
  bytes(): void
  picture(picture: TablePicture): void
}

export async function streamTable(
  campaignId: string,
  signal: AbortSignal,
  request: typeof fetch,
  updates: StreamUpdates,
): Promise<StreamEnd> {
  try {
    const response = await request(`/table/stream?campaign_id=${encodeURIComponent(campaignId)}`, {
      headers: { Accept: 'text/event-stream' },
      cache: 'no-store',
      signal,
    })
    if (signal.aborted) return { kind: 'retry' }
    if (response.status === 401) return { kind: 'signed-out' }
    const retryAfter = response.headers.get('Retry-After')
    if (response.status === 503 && retryAfter !== null) {
      const seconds = Number(retryAfter)
      return { kind: 'full', delay: Number.isFinite(seconds) ? Math.max(1000, Math.min(seconds * 1000, 300_000)) : 5000 }
    }
    const contentType = response.headers.get('Content-Type')?.split(';', 1)[0].trim().toLowerCase()
    if (!response.ok || response.body === null || contentType !== 'text/event-stream') return { kind: 'retry' }
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
        if (picture !== null) end = { kind: 'reconnect' }
        return false
      }
      if (picture === null) {
        if (
          pending.length >= SNAPSHOT_FRAME_LIMIT || frame.event === 'slot'
          || (frame.event === 'snapshot' && pending.some((event) => event.event === 'snapshot'))
        ) return false
        pending.push(frame)
        if (frame.event !== 'ready') return true
        const snapshot = tablePicture({ schema_version: CONTRACT_VERSION, frames: pending })
        pending.length = 0
        if (snapshot.kind !== 'live') return false
        picture = snapshot
        updates.picture(picture)
        return true
      }
      if (frame.event === 'audio') return true
      if (frame.event !== 'slot') return false
      const previous = picture.slots.find((slot) => slot.slot === frame.slot)
      if (previous === undefined) return false
      if (frame.seq <= previous.seq) return true
      if (frame.seq !== previous.seq + 1) {
        end = { kind: 'reconnect' }
        return false
      }
      const next: TableSlot = { slot: frame.slot, seq: frame.seq, content: frame.content }
      picture = { ...picture, slots: picture.slots.map((slot) => slot.slot === next.slot ? next : slot) }
      updates.picture(picture)
      return true
    })
    return end
  } catch {
    return { kind: 'retry' }
  }
}
